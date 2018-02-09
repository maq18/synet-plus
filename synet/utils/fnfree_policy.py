"""
Synthesize policies .. aka route maps for the moment
"""

import copy
import functools
import itertools
import z3

from synet.topo.bgp import Announcement
from synet.topo.bgp import Community
from synet.topo.bgp import Match
from synet.topo.bgp import MatchPeer
from synet.topo.bgp import MatchLocalPref
from synet.topo.bgp import MatchCommunitiesList
from synet.topo.bgp import MatchNextHop
from synet.topo.bgp import MatchIpPrefixListList
from synet.utils.fnfree_smt_context import ASPATH_SORT
from synet.utils.fnfree_smt_context import BGP_ORIGIN_SORT
from synet.utils.fnfree_smt_context import PEER_SORT
from synet.utils.fnfree_smt_context import PREFIX_SORT
from synet.utils.fnfree_smt_context import NEXT_HOP_SORT
from synet.utils.fnfree_smt_context import SMTVar
from synet.utils.fnfree_smt_context import SolverContext
from synet.utils.fnfree_smt_context import is_symbolic
from synet.utils.fnfree_smt_context import is_empty


__author__ = "Ahmed El-Hassany"
__email__ = "a.hassany@gmail.com"


class SMTAbstractMatch(object):
    """Generic Match Class"""

    def is_match(self, announcement):
        """
        Returns a Var that is evaluated when partial evaluation is possible.
        Using this method on the same announcement multiple times generates
        redundant constraints and variables
        """
        raise NotImplementedError()


class SMTMatchAll(SMTAbstractMatch):
    """Matches all announcements regardless of their contents"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.match_var = ctx.create_fresh_var(
            z3.BoolSort(), name_prefix='match_all_', value=True)

    def is_match(self, announcement):
        return self.match_var


class SMTMatchNone(SMTAbstractMatch):
    """Does NOT match any announcement regardless of its contents"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.match_var = ctx.create_fresh_var(
            z3.BoolSort(), name_prefix='match_none_', value=False)

    def is_match(self, announcement):
        return self.match_var


class SMTMatchAnd(SMTAbstractMatch):
    """Combine Matches in `Or` expression"""

    def __init__(self, matches, announcements, ctx):
        self.matches = matches
        self.announcements = announcements
        self.ctx = ctx
        self.matched_announcements = {}  # Cache evaluated announcements

    def is_match(self, announcement):
        # Check cache first
        # TODO partially evaluate short cuts
        if announcement not in self.matched_announcements:
            results = [match.is_match(announcement) for match in self.matches]
            is_concrete = all([result.is_concrete for result in results])
            value = None
            if is_concrete:
                value = all([result.get_value() for result in results])
            match_var = self.ctx.create_fresh_var(
                z3.BoolSort(), name_prefix='match_and_', value=value)
            if not is_concrete:
                constraint = z3.And([result.var == True for result in results])
                self.ctx.register_constraint(
                    match_var.var == constraint, name_prefix='const_and_')
            self.matched_announcements[announcement] = match_var
        return self.matched_announcements[announcement]


class SMTMatchOr(SMTAbstractMatch):
    """Combine Matches in Or expression"""

    def __init__(self, matches, announcements, ctx):
        """
        :param matches: List of SMTMatches
        :param announcements:
        :param ctx:
        """
        self.matches = matches
        self.announcements = announcements
        self.ctx = ctx
        self.matched_announcements = {}  # Cache evaluated announcements

    def is_match(self, announcement):
        # Check cache first
        # TODO partially evaluate short cuts
        if announcement not in self.matched_announcements:
            results = [match.is_match(announcement) for match in self.matches]
            is_concrete = all([result.is_concrete for result in results])
            value = None
            if is_concrete:
                value = any([result.get_value() for result in results])
            match_var = self.ctx.create_fresh_var(
                z3.BoolSort(), name_prefix='match_or_', value=value)
            if not is_concrete:
                constraint = z3.Or([result.var == True for result in results])
                self.ctx.register_constraint(
                    match_var.var == constraint, name_prefix='const_or_')
            self.matched_announcements[announcement] = match_var
        return self.matched_announcements[announcement]


class SMTMatchSelectOne(SMTAbstractMatch):
    """
    Chose a SINGLE match object to meet the requirements
    """

    def __init__(self, announcements, ctx, matches=None):
        """
        :param announcements:
        :param ctx:
        :param matches: List of SMTMatch objects to use one of them
                        if None, then all attributes are going to be used.
        """
        assert isinstance(ctx, SolverContext)
        assert announcements, 'Cannot match on empty announcements'
        self.announcements = announcements
        self.ctx = ctx
        self.matched_announcements = {}  # Cache evaluated announcements

        if not matches:
            # By default all attributes are allowed
            matches = []
            for attr in Announcement.attributes:
                if attr == 'communities':
                    for community in self.announcements[0].communities:
                        # Match only when community is set
                        match = attribute_match_factory(
                            community,
                            value=None,
                            announcements=self.announcements,
                            ctx=self.ctx)
                        matches.append(match)
                else:
                    # Symbolic match value
                    match = attribute_match_factory(
                        attr,
                        value=None,
                        announcements=self.announcements,
                        ctx=self.ctx)
                    matches.append(match)

        # Create map for the different matches
        self.matches = {}
        self.index_var = self.ctx.create_fresh_var(
            z3.IntSort(), name_prefix='SelectOne_index_')
        for index, match in enumerate(matches):
            self.matches[index] = match
        # Make index in the range of number of matches
        self.ctx.register_constraint(
            z3.And(
                self.index_var >= 0,
                self.index_var.var < index + 1),
            name_prefix='SelectOne_index_range_')

    def _get_match(self, announcement, current_index=0):
        """Recursively construct a match"""
        if current_index not in self.matches:
            # Base case
            return z3.And(self.index_var.var == current_index, False)
        match_var = self.matches[current_index].is_match(announcement).var
        index_check = self.index_var.var == current_index
        next_attr = self._get_match(announcement, current_index + 1)
        return z3.If(index_check, match_var, next_attr)

    def is_match(self, announcement):
        if announcement not in self.matched_announcements:
            var = self.ctx.create_fresh_var(z3.BoolSort())
            self.matched_announcements[announcement] = var
            constraint = var.var == self._get_match(announcement)
            self.ctx.register_constraint(
                constraint, name_prefix='SelectOne_match_')
        return self.matched_announcements[announcement]

    def get_used_match(self):
        match = self.matches[self.index_var.get_value()]
        return match


class SMTMatchAttribute(SMTAbstractMatch):
    """Match on a single attribute of announcement"""

    def __init__(self, attribute, value, announcements, ctx):
        """
        :param attribute: Must be in Announcement.attributes
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars
        """
        super(SMTMatchAttribute, self).__init__()
        assert isinstance(ctx, SolverContext)
        assert announcements, 'Cannot match on empty announcements'
        assert attribute in Announcement.attributes
        if value is None:
            asort = getattr(announcements[0], attribute).vsort
            value = ctx.create_fresh_var(
                asort,
                name_prefix='Match_attr_%s_' % attribute)
        assert isinstance(value, SMTVar)
        attr_sort = getattr(announcements[0], attribute).vsort
        err = "Type mismatch of attribute and value %s != %s" % (
            attr_sort, value.vsort)
        assert attr_sort == value.vsort, err
        self.attribute = attribute
        self.value = value
        self.announcements = announcements
        self.ctx = ctx
        self.matched_announcements = {}  # Cache evaluated announcements

    def is_match(self, announcement):
        attr = getattr(announcement, self.attribute)
        # Check cache first
        if announcement not in self.matched_announcements:
            constraint = attr.check_eq(self.value)
            value = None
            if not is_symbolic(constraint):
                value = constraint
            match_var = self.ctx.create_fresh_var(
                z3.BoolSort(),
                name_prefix='match_%s_var_' % self.attribute,
                value=value)
            if is_symbolic(constraint):
                self.ctx.register_constraint(
                    match_var.var == constraint,
                    name_prefix='const_match_%s_' % self.attribute)
            self.matched_announcements[announcement] = match_var
        return self.matched_announcements[announcement]


class SMTMatchCommunity(SMTAbstractMatch):
    """Match if a single community value is set to True"""

    def __init__(self, community, value, announcements, ctx):
        """

        :param community:
        :param value: Optionally can be None, then set by default to True
        :param announcements:
        :param ctx:
        """
        assert isinstance(ctx, SolverContext)
        assert announcements, "Cannot match on empty announcements"
        assert community in announcements[0].communities
        if not value:
            value = ctx.create_fresh_var(
                z3.BoolSort(),
                name_prefix='Match_Community_var_',
                value=True)
        assert isinstance(value, SMTVar)
        self.ctx = ctx
        self.value = value
        self.community = community
        self.announcements = announcements
        self.matched_announcements = {}  # Cache evaluated announcements

    def is_match(self, announcement):
        if announcement not in self.matched_announcements:
            attr = announcement.communities[self.community]
            constraint = attr.check_eq(self.value)
            value = None
            if not is_symbolic(constraint):
                value = constraint
            match_var = self.ctx.create_fresh_var(z3.BoolSort(), value=value)
            if is_symbolic(constraint):
                self.ctx.register_constraint(match_var.var == constraint)
            self.matched_announcements[announcement] = match_var
        return self.matched_announcements[announcement]


class SMTMatchPrefix(SMTMatchAttribute):
    """Matches Announcement.prefix"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars
        """
        super(SMTMatchPrefix, self).__init__('prefix', value, announcements, ctx)


class SMTMatchPeer(SMTMatchAttribute):
    """Short cut to match on Announcement.peer"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars
        """
        super(SMTMatchPeer, self).__init__('peer', value, announcements, ctx)


class SMTMatchOrigin(SMTMatchAttribute):
    """Short cut to match on Announcement.origin"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchOrigin, self).__init__('origin', value, announcements, ctx)


class SMTMatchNextHop(SMTMatchAttribute):
    """Short cut to match on Announcement.next_hop"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchNextHop, self).__init__(
            'next_hop', value, announcements, ctx)


class SMTMatchASPath(SMTMatchAttribute):
    """Short cut to match on Announcement.as_path"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchASPath, self).__init__('as_path', value, announcements, ctx)


class SMTMatchASPathLen(SMTMatchAttribute):
    """Short cut to match on Announcement.as_path_len"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchASPathLen, self).__init__('as_path_len', value, announcements, ctx)


class SMTMatchLocalPref(SMTMatchAttribute):
    """Short cut to match on Announcement.local_pref"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchLocalPref, self).__init__('local_pref', value, announcements, ctx)


class SMTMatchMED(SMTMatchAttribute):
    """Short cut to match on Announcement.med"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchMED, self).__init__('med', value, announcements, ctx)


class SMTMatchPermitted(SMTMatchAttribute):
    """Short cut to match on Announcement.permitted"""

    def __init__(self, value, announcements, ctx):
        """
        :param value: Symbolic Var, or None to create one by default
        :param announcements: List of announcements
        :param ctx: to register new constraints and create fresh vars"""
        super(SMTMatchPermitted, self).__init__(
            'permitted', value, announcements, ctx)


class SMTAction(object):
    """Parent action class"""

    @property
    def old_announcements(self):
        raise NotImplementedError()

    @property
    def announcements(self):
        raise NotImplementedError()

    @property
    def attributes(self):
        """Set of attributes affected by this action"""
        raise NotImplementedError()

    @property
    def communities(self):
        """Set of communities affected by this action"""
        raise NotImplementedError()

    def execute(self):
        """Partial evaluate the action and generate new announcements set"""
        raise NotImplementedError()


class SMTSetAttribute(SMTAction):
    """Action to change one attribute in the announcement"""

    def __init__(self, match, attribute, value, announcements, ctx):
        super(SMTSetAttribute, self).__init__()
        assert isinstance(ctx, SolverContext)
        assert attribute in Announcement.attributes
        assert hasattr(match, 'is_match')
        assert announcements
        if value is None:
            vsort = getattr(announcements[0], attribute).vsort
            prefix = 'Set_%s_val' % attribute
            value = ctx.create_fresh_var(vsort, name_prefix=prefix)
        assert isinstance(value, SMTVar)
        attr_sort = getattr(announcements[0], attribute).vsort
        err = "Type mismatch of attribute and value %s != %s" % (
            attr_sort, value.vsort)
        assert attr_sort == value.vsort, err
        self.match = match
        self.attribute = attribute
        self.value = value
        self._old_announcements = announcements
        self._announcements = None
        self.smt_ctx = ctx
        self.execute()

    @property
    def announcements(self):
        return self._announcements

    @property
    def old_announcements(self):
        return self._old_announcements

    @property
    def attributes(self):
        return set([self.attribute])

    @property
    def communities(self):
        return set([])

    def execute(self):
        if self._announcements:
            return
        constraints = []
        announcements = []
        for announcement in self._old_announcements:
            new_vals = {}
            for attr in announcement.attributes:
                attr_var = getattr(announcement, attr)
                if attr == self.attribute:
                    is_match = self.match.is_match(announcement)
                    if is_match.is_concrete:
                        if is_match.get_value():
                            new_var = self.value
                        else:
                            new_var = getattr(announcement, attr)
                    else:
                        new_var = self.smt_ctx.create_fresh_var(
                            attr_var.vsort, value=self.value,
                            name_prefix='Action%sVal' % attr)
                        constraint = z3.If(is_match.var,
                                           new_var.var == self.value,
                                           new_var.var == attr_var.var)
                        constraints.append(constraint)
                    new_vals[attr] = new_var
                else:
                    new_vals[attr] = attr_var
            new_ann = Announcement(**new_vals)
            announcements.append(new_ann)
        if constraints:
            self.smt_ctx.register_constraint(z3.And(*constraints))
        self._announcements = self._old_announcements.create_new(announcements, self)


class SMTSetCommunity(SMTAction):
    """Action to change one attribute in the announcement"""

    def __init__(self, match, community, value, announcements, ctx):
        super(SMTSetCommunity, self).__init__()
        assert isinstance(ctx, SolverContext)
        assert hasattr(match, 'is_match')
        assert community in announcements[0].communities
        assert announcements
        if value is None:
            prefix = 'Set_community_val_'
            value = ctx.create_fresh_var(
                z3.BoolSort(), name_prefix=prefix, value=True)
        assert isinstance(value, SMTVar)
        err = "Value is not of type BoolSort %s" % (value.vsort)
        assert z3.BoolSort() == value.vsort, err
        self.match = match
        self.community = community
        self.value = value
        self._old_announcements = announcements
        self._announcements = None
        self.smt_ctx = ctx
        self.execute()

    @property
    def announcements(self):
        return self._announcements

    @property
    def old_announcements(self):
        return self._old_announcements

    @property
    def attributes(self):
        return set(['communities'])

    @property
    def communities(self):
        return set([self.community])

    def execute(self):
        if self._announcements:
            return
        constraints = []
        announcements = []
        for announcement in self._old_announcements:
            new_vals = {}
            for attr in announcement.attributes:
                attr_var = getattr(announcement, attr)
                if attr != 'communities':
                    # Other attributes stay the same
                    new_vals[attr] = attr_var
                else:
                    new_comms = {}
                    for community, old_var in announcement.communities.iteritems():
                        if community != self.community:
                            # Other communities stay the same
                            new_comms[community] = old_var
                        else:
                            is_match = self.match.is_match(announcement)
                            if is_match.is_concrete:
                                # Partial eval
                                new_var = self.value if is_match.get_value() else old_var
                            else:
                                # No partial eval
                                new_var = self.smt_ctx.create_fresh_var(
                                    z3.BoolSort(), value=self.value,
                                    name_prefix='set_community_%s_val' % attr)
                                constraint = z3.If(is_match.var,
                                                   new_var.var == self.value,
                                                   new_var.var == attr_var.var)
                                constraints.append(constraint)
                            new_comms[community] = new_var
                    new_vals[attr] = new_comms
            new_ann = Announcement(**new_vals)
            announcements.append(new_ann)
        if constraints:
            self.smt_ctx.register_constraint(z3.And(*constraints))
        self._announcements = self._old_announcements.create_new(announcements, self)


class SMTSetOne(SMTAction):
    """
    Chose a SINGLE match object to meet the requirements
    """

    def __init__(self, match, announcements, ctx, actions=None):
        """
        :param announcements:
        :param ctx:
        :param actions: List of SMTMatch objects to use one of them
                        if None, then all attributes are going to be used.
        """
        super(SMTSetOne, self).__init__()
        assert isinstance(ctx, SolverContext)
        assert announcements, 'Cannot match on empty announcements'
        self._old_announcements = announcements
        self._announcements = None
        self.ctx = ctx
        self.match = match

        if not actions:
            # By default all attributes are allowed
            actions = []
            for attr in Announcement.attributes:
                if attr == 'communities':
                    for community in self.old_announcements[0].communities:
                        action = attribute_set_factory(
                            community, match, None,
                            self.old_announcements, self.ctx)
                        actions.append(action)
                else:
                    # Extract he z3 type of the given attribute
                    action = attribute_set_factory(
                        attr, match, None, self.old_announcements, self.ctx)
                    actions.append(action)

        # Create map for the different actions
        self.actions = {}
        self.index_var = self.ctx.create_fresh_var(
            z3.IntSort(), name_prefix='SetOneIndex_')
        index = itertools.count(0)
        for action in actions:
            err1 = 'All actions must have the same match'
            assert action.match == self.match, err1
            err2 = 'All actions must have the same announcements'
            assert action.old_announcements == self.old_announcements, err2
            self.actions[index.next()] = action
        # Make index in the range of number of actions
        index_range = z3.And(self.index_var.var >= 0,
                             self.index_var.var < index.next())
        self.ctx.register_constraint(index_range,
                                     name_prefix='setone_index_max_')

    @property
    def old_announcements(self):
        return self._old_announcements

    @property
    def announcements(self):
        return self._announcements

    @property
    def attributes(self):
        return reduce(
            set.union,
            [getattr(a, 'attributes', set([None])) for a in self.actions.values()])

    @property
    def communities(self):
        return reduce(
            set.union,
            [getattr(a, 'communities') for a in self.actions.values()])

    def _get_actions(self, ann_index, attribute, default, index=0):
        """
        Recursively construct a match for an attribute (other than communities
        """
        if index not in self.actions:
            # Base case
            return default
        action = self.actions[index]
        value = getattr(action.announcements[ann_index], attribute)
        index_check = self.index_var.var == index
        next_attr = self._get_actions(ann_index, attribute, default, index + 1)
        return z3.If(index_check, value.var, next_attr)

    def _get_communities(self, ann_index, community, default, index=0):
        """Recursively construct a match for a given community"""
        if index not in self.actions:
            # Base case
            return default
        action = self.actions[index]
        value = action.announcements[ann_index].communities[community]
        index_check = self.index_var.var == index
        next_attr = self._get_communities(
            ann_index, community, default, index + 1)
        return z3.If(index_check, value.var, next_attr)

    def execute(self):
        new_anns = []
        # Execute the previous actions
        for action in self.actions.values():
            action.execute()
        # IF all previous actions are simple Attribute setters
        # then partial eval is more possible
        attr_only = None not in self.attributes
        for index, old_ann in enumerate(self.old_announcements):
            new_values = {}
            for attr in Announcement.attributes:
                old_var = getattr(old_ann, attr)
                # Parial evaluation
                if attr_only and attr not in self.attributes:
                    # This attribute is not changed by any of the actions
                    # Thus stays the same
                    new_values[attr] = old_var
                else:
                    # This attribute can be changed by at least one action
                    if attr == 'communities':
                        # Shallow copy
                        new_comms = copy.copy(getattr(old_ann, attr))
                        for community in self.communities:
                            prefix = 'setone_community_var_'
                            new_var = self.ctx.create_fresh_var(
                                z3.BoolSort(), name_prefix=prefix)
                            value = self._get_communities(
                                index, community, new_var.var)
                            prefix = 'setone_%s_' % attr
                            self.ctx.register_constraint(
                                new_var.var == value, name_prefix=prefix)
                            new_comms[community] = new_var
                        new_values[attr] = new_comms
                    else:
                        prefix = 'setone_%s_var_' % attr
                        new_var = self.ctx.create_fresh_var(
                            old_var.vsort, name_prefix=prefix)
                        value = self._get_actions(index, attr, new_var.var)
                        prefix = 'setone_%s_' % attr
                        self.ctx.register_constraint(
                            new_var.var == value, name_prefix=prefix)
                        new_values[attr] = new_var
            new_anns.append(Announcement(**new_values))
        self._announcements = self.old_announcements.create_new(new_anns, self)

    def get_used_action(self):
        """Return the used action object"""
        match = self.actions[self.index_var.get_value()]
        return match


class SMTSetLocalPref(SMTSetAttribute):
    """Short cut to set the value of Announcement.local_pref"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetLocalPref, self).__init__(
            match, 'local_pref', value, announcements, ctx)


class SMTSetPrefix(SMTSetAttribute):
    """Short cut to set the value of Announcement.prefix"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetPrefix, self).__init__(
            match, 'prefix', value, announcements, ctx)


class SMTSetPeer(SMTSetAttribute):
    """Short cut to set the value of Announcement.peer"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetPeer, self).__init__(
            match, 'peer', value, announcements, ctx)


class SMTSetOrigin(SMTSetAttribute):
    """Short cut to set the value of Announcement.origin"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetOrigin, self).__init__(
            match, 'origin', value, announcements, ctx)


class SMTSetPermitted(SMTSetAttribute):
    """Short cut to set the value of Announcement.permitted"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetPermitted, self).__init__(
            match, 'permitted', value, announcements, ctx)


class SMTSetASPath(SMTSetAttribute):
    """Short cut to set the value of Announcement.as_path"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetASPath, self).__init__(
            match, 'as_path', value, announcements, ctx)


class SMTSetASPathLen(SMTSetAttribute):
    """Short cut to set the value of Announcement.as_path_len"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetASPathLen, self).__init__(
            match, 'as_path_len', value, announcements, ctx)


class SMTSetNextHop(SMTSetAttribute):
    """Short cut to set the value of Announcement.next_hop"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetNextHop, self).__init__(
            match, 'next_hop', value, announcements, ctx)


class SMTSetMED(SMTSetAttribute):
    """Short cut to set the value of Announcement.med"""

    def __init__(self, match, value, announcements, ctx):
        """
        :param match: SMTMatch object
        :param value: Symbolic Var, or None to create one by default
        :param announcements: AnnouncementsContext
        :param ctx: SolverContext
        """
        super(SMTSetMED, self).__init__(
            match, 'med', value, announcements, ctx)


def attribute_match_factory(attribute, value=None, announcements=None, ctx=None):
    """
    Given an attribute name or Community value return the right match class
    If announcements and ctx are set, then a concrete object is returned
    """
    match_map = {
        'prefix': SMTMatchPrefix,
        'peer': SMTMatchPeer,
        'origin': SMTMatchOrigin,
        'as_path': SMTMatchASPath,
        'as_path_len': SMTMatchASPathLen,
        'next_hop': SMTMatchNextHop,
        'local_pref': SMTMatchLocalPref,
        'permitted': SMTMatchPermitted,
        'med': SMTMatchMED,
    }
    if attribute in match_map:
        klass = match_map[attribute]
    elif isinstance(attribute, Community):
        klass = functools.partial(SMTMatchCommunity, community=attribute)
    else:
        raise ValueError("Unrecognized attribute or community '%s'" % attribute)

    if announcements and ctx:
        return klass(value=value, announcements=announcements, ctx=ctx)
    return klass


def attribute_set_factory(attribute, match=None, value=None, announcements=None, ctx=None):
    """
    Given an attribute name or Community value return the right match class
    If announcements and ctx are set, then a concrete object is returned
    """
    match_map = {
        'prefix': SMTSetPrefix,
        'peer': SMTSetPeer,
        'origin': SMTSetOrigin,
        'as_path': SMTSetASPath,
        'as_path_len': SMTSetASPathLen,
        'next_hop': SMTSetNextHop,
        'local_pref': SMTSetLocalPref,
        'permitted': SMTSetPermitted,
        'med': SMTSetMED,
    }
    if attribute in match_map:
        klass = match_map[attribute]
    elif isinstance(attribute, Community):
        klass = functools.partial(SMTSetCommunity, community=attribute)
    else:
        raise ValueError("Unrecognized attribute or community '%s'" % attribute)

    if match and announcements and ctx:
        return klass(match=match, value=value, announcements=announcements, ctx=ctx)
    return klass


class SMTMatch(SMTAbstractMatch):
    def __init__(self, match, announcements, ctx):
        assert isinstance(match, Match)
        self.match = match
        self.announcements = announcements
        self.ctx = ctx
        self.smt_match = None
        self.value = None
        self.match_dispatch = {
            MatchNextHop: self._load_match_next_hop,
            MatchIpPrefixListList: self._load_match_prefix_list,
            MatchCommunitiesList: self._load_match_communities_list,
            MatchLocalPref: self._load_match_local_pref,
            MatchPeer: self._load_match_peer,
        }
        self.match_dispatch[type(match)]()

    def is_match(self, announcement):
        return self.smt_match.is_match(announcement)

    def _load_match_next_hop(self):
        value = self.match.match if not is_empty(self.match.match) else None
        vsort = self.ctx.get_enum_type(NEXT_HOP_SORT)
        if value:
            value = vsort.get_symbolic_value(value)
        self.value = self.ctx.create_fresh_var(vsort=vsort, value=value)
        self.smt_match = SMTMatchNextHop(self.value, self.announcements, self.ctx)

    def _load_match_local_pref(self):
        value = self.match.match if not is_empty(self.match.match) else None
        self.value = self.ctx.create_fresh_var(vsort=z3.IntSort(), value=value)
        self.smt_match = SMTMatchLocalPref(self.value, self.announcements, self.ctx)

    def _load_match_peer(self):
        value = self.match.match if not is_empty(self.match.match) else None
        vsort = self.ctx.get_enum_type(PEER_SORT)
        if value:
            value = vsort.get_symbolic_value(value)
        self.value = self.ctx.create_fresh_var(vsort=vsort, value=value)
        self.smt_match = SMTMatchPeer(self.value, self.announcements, self.ctx)

    def _get_ip_match(self, ip):
        vsort = self.ctx.get_enum_type(PREFIX_SORT)
        if not is_empty(ip):
            val = vsort.get_symbolic_value(ip)
            var = self.ctx.create_fresh_var(vsort, value=val)
            return SMTMatchPrefix(var, self.announcements, self.ctx)
        else:
            matches = []
            for ip in vsort.symbolic_values:
                var = self.ctx.create_fresh_var(vsort, value=ip)
                m = SMTMatchPrefix(var, self.announcements, self.ctx)
                matches.append(m)
            return SMTMatchSelectOne(self.announcements, self.ctx, matches)

    def _load_match_prefix_list(self):
        matches = []
        for community in self.match.match.networks:
            match = self._get_ip_match(community)
            matches.append(match)
        self.smt_match = SMTMatchAnd(matches, self.announcements, self.ctx)

    def _get_community_match(self, community):
        if not is_empty(community):
            var = self.ctx.create_fresh_var(vsort=z3.BoolSort(), value=True)
            match = SMTMatchCommunity(community=community, value=var,
                                      announcements=self.announcements,
                                      ctx=self.ctx)
        else:
            comms = []
            for comm in self.ctx.communities:
                var = self.ctx.create_fresh_var(z3.BoolSort(), value=True)
                smt = SMTMatchCommunity(comm, var, self.announcements, self.ctx)
                comms.append(smt)
            match = SMTMatchSelectOne(self.announcements, self.ctx, comms)
        return match

    def _load_match_communities_list(self):
        matches = []
        for community in self.match.match.communities:
            match = self._get_community_match(community)
            matches.append(match)
        self.smt_match = SMTMatchAnd(matches, self.announcements, self.ctx)
