from __future__ import absolute_import

import collections
from itertools import chain
import re
import types
import unicodedata

from richenum import OrderedRichEnum
from richenum import OrderedRichEnumValue

from normalize.property import SafeProperty
from normalize.coll import Collection
from normalize.coll import ListCollection
import normalize.exc as exc
from normalize.record import Record
from normalize.record import record_id
from normalize.selector import FieldSelector
from normalize.selector import MultiFieldSelector


class DiffTypes(OrderedRichEnum):
    """
    A :py:class:`richenum.OrderedRichEnum` type to denote the type of an
    individual difference.
    """
    class EnumValue(OrderedRichEnumValue):
        pass

    NO_CHANGE = EnumValue(1, "none", "UNCHANGED")
    ADDED = EnumValue(2, "added", "ADDED")
    REMOVED = EnumValue(3, "removed", "REMOVED")
    MODIFIED = EnumValue(4, "modified", "MODIFIED")


def _coerce_diff(dt):
    if not isinstance(dt, OrderedRichEnumValue):
        if isinstance(dt, (int, long)):
            dt = DiffTypes.from_index(dt)
        else:
            dt = DiffTypes.from_canonical(dt)
    return dt


class DiffInfo(Record):
    """
    Container for storing diff information that can be used to reconstruct the
    values diffed.
    """
    diff_type = SafeProperty(
        coerce=_coerce_diff,
        isa=DiffTypes.EnumValue,
        required=True,
        doc="Enumeration describing the type of difference; a "
            ":py:class:`DiffType` value.")
    base = SafeProperty(isa=FieldSelector, required=True)
    other = SafeProperty(isa=FieldSelector, required=True)

    def __str__(self):
        if self.base.path != self.other.path:
            pathinfo = (
                self.base.path if (
                    len(self.base) > len(self.other) and
                    self.base.startswith(self.other)
                ) else self.other.path if (
                    len(self.other) > len(self.base) and
                    self.other.startswith(self.base)
                ) else "(%s/%s)" % (self.base.path, self.other.path)
            )
        else:
            pathinfo = self.other.path
        difftype = self.diff_type.display_name
        return "<DiffInfo: %s %s>" % (difftype, pathinfo)


class _Nothing(object):
    def __repr__(self):
        return "(not set)"


_nothing = _Nothing()


class DiffOptions(object):
    """Optional data structure to pass diff options down.  Some functions are
    delegated to this object, allowing for further customization of operation,
    using a sub-class API.
    """
    _nothing = _nothing

    def __init__(self, ignore_ws=True, ignore_case=False,
                 unicode_normal=True, unchanged=False,
                 ignore_empty_slots=False,
                 duck_type=False, extraneous=False,
                 compare_filter=None):
        """Create a new ``DiffOptions`` instance.

        args:

            ``ignore_ws=``\ *BOOL*
                Ignore whitespace in strings (beginning, end and middle).
                True by default.

            ``ignore_case=``\ *BOOL*
                Ignore case differences in strings.  False by default.

            ``unicode_normal=``\ *BOOL*
                Ignore unicode normal form differences in strings by
                normalizing to NFC before comparison.  True by default.

            ``unchanged=``\ *BOOL*
                Yields ``DiffInfo`` objects for every comparison, not just
                those which found a difference.  Defaults to False.  Useful for
                testing.

            ``ignore_empty_slots=``\ *BOOL*
                If true, slots containing typical 'empty' values (by default,
                just ``''`` and ``None``) are treated as if they were not set.
                False by default.

            ``duck_type=``\ *BOOL*
                Normally, types must match or the result will always be
                :py:attr:`normalize.diff.DiffTypes.MODIFIED` and the comparison
                will not descend further.

                However, setting this option bypasses this check, and just
                checks that the 'other' object has all of the properties
                defined on the 'base' type.  This can be used to check progress
                when porting from other object systems to normalize.

            ``compare_filter=``\ *MULTIFIELDSELECTOR*\ \|\ *LIST_OF_LISTS*
                Restrict comparison to the fields described by the passed
                :py:class:`MultiFieldSelector` (or list of FieldSelector
                lists/objects)
        """
        self.ignore_ws = ignore_ws
        self.ignore_case = ignore_case
        self.ignore_empty_slots = ignore_empty_slots
        self.unicode_normal = unicode_normal
        self.unchanged = unchanged
        self.duck_type = duck_type
        self.extraneous = extraneous
        if isinstance(compare_filter, (MultiFieldSelector, types.NoneType)):
            self.compare_filter = compare_filter
        else:
            self.compare_filter = MultiFieldSelector(*compare_filter)

    def items_equal(self, a, b):
        """Sub-class hook which performs value comparison.  Only called for
        comparisons which are not Records."""
        return a == b

    def normalize_whitespace(self, value):
        """Normalizes whitespace; called if ``ignore_ws`` is true."""
        if isinstance(value, unicode):
            return u" ".join(
                x for x in re.split(r'\s+', value, flags=re.UNICODE) if
                len(x)
            )
        else:
            return " ".join(value.split())

    def normalize_unf(self, value):
        """Normalizes Unicode Normal Form (to NFC); called if
        ``unicode_normal`` is true."""
        if isinstance(value, unicode):
            return unicodedata.normalize('NFC', value)
        else:
            return value

    def normalize_case(self, value):
        """Normalizes Case (to upper case); called if ``ignore_case`` is
        true."""
        # FIXME: this will do the wrong thing for letters in some languages, eg
        # Greek, Turkish.  Correct, locale-dependent unicode case folding is
        # left as an exercise for a subclass.
        return value.upper()

    def value_is_empty(self, value):
        """This method decides whether the value is 'empty', and hence the same
        as not specified.  Called if ``ignore_empty_slots`` is true.  Checking
        the value for emptiness happens *after* all other normalization.
        """
        return (not value and isinstance(value, (basestring, types.NoneType)))

    def normalize_text(self, value):
        """This hook is called by :py:meth:`DiffOptions.normalize_val` if the
        value (after slot/item normalization) is a string, and is responsible
        for calling the various ``normalize_``\ foo methods which act on text.
        """
        if self.ignore_ws:
            value = self.normalize_whitespace(value)
        if self.ignore_case:
            value = self.normalize_case(value)
        if self.unicode_normal:
            value = self.normalize_unf(value)
        return value

    def normalize_val(self, value=_nothing):
        """Hook which is called on every value before comparison, and should
        return the scrubbed value or ``self._nothing`` to indicate that the
        value is not set.
        """
        if isinstance(value, basestring):
            value = self.normalize_text(value)
        if self.ignore_empty_slots and self.value_is_empty(value):
            value = _nothing
        return value

    def normalize_slot(self, value=_nothing, prop=None):
        """Hook which is called on every *record slot*; this is a way to
        perform context-aware clean-ups.

        args:

            ``value=``\ *nothing*\ \|\ *anything*
                The value in the slot.  *nothing* can be detected in sub-class
                methods as ``self._nothing``.

            ``prop=``\ *PROPERTY*
                The slot's :py:class:`normalize.property.Property` instance.
                If this instance has a ``compare_as`` method, then that method
                is called to perform a clean-up before the value is passed to
                ``normalize_val``
        """
        if value is not _nothing and hasattr(prop, "compare_as"):
            value = prop.compare_as(value)
        return self.normalize_val(value)

    def normalize_item(self, value=_nothing, coll=None, index=None):
        """Hook which is called on every *collection item*; this is a way to
        perform context-aware clean-ups.

        args:

            ``value=``\ *nothing*\ \|\ *anything*
                The value in the collection slot.  *nothing* can be detected in
                sub-class methods as ``self._nothing``.

            ``coll=``\ *COLLECTION*
                The parent :py:class:`normalize.coll.Collection` instance.  If
                this instance has a ``compare_item_as`` method, then that
                method is called to perform a clean-up before the value is
                passed to ``normalize_val``

            ``index=``\ *HASHABLE*
                The key of the item in the collection.
        """
        if value is not _nothing and hasattr(coll, "compare_item_as"):
            value = coll.compare_item_as(value)
        return self.normalize_val(value)

    def record_id(self, record, type_=None, selector=None):
        """Retrieve an object identifier from the given record; if it is an
        alien class, and the type is provided, then use duck typing to get the
        corresponding fields of the alien class."""
        pk = record_id(record, type_, selector, self.normalize_slot)
        return pk

    def id_args(self, type_, fs):
        options = dict()
        if self.duck_type:
            options['type_'] = type_
        if self.compare_filter:
            options['selector'] = self.compare_filter[fs][any]
        return options

    def is_filtered(self, fs):
        return self.compare_filter and fs not in self.compare_filter


def compare_record_iter(a, b, fs_a=None, fs_b=None, options=None):
    if not options:
        options = DiffOptions()

    if not options.duck_type and type(a) != type(b):
        raise TypeError(
            "cannot compare %s with %s" % (type(a).__name__, type(b).__name__)
        )

    if fs_a is None:
        fs_a = FieldSelector(tuple())
        fs_b = FieldSelector(tuple())

    properties = type(a).properties
    for propname in sorted(properties):

        if options.is_filtered(fs_a + propname):
            continue

        prop = properties[propname]
        if prop.extraneous and not options.extraneous:
            continue

        propval_a = options.normalize_slot(
            getattr(a, propname, _nothing), prop,
        )
        propval_b = options.normalize_slot(
            getattr(b, propname, _nothing), prop,
        )

        if propval_a is _nothing and propval_b is _nothing:
            # don't yield NO_CHANGE for fields missing on both sides
            continue
        elif propval_a is _nothing and propval_b is not _nothing:
            yield DiffInfo(
                diff_type=DiffTypes.ADDED,
                base=fs_a + [propname],
                other=fs_b + [propname],
            )
        elif propval_b is _nothing and propval_a is not _nothing:
            yield DiffInfo(
                diff_type=DiffTypes.REMOVED,
                base=fs_a + [propname],
                other=fs_b + [propname],
            )
        elif (options.duck_type or type(propval_a) == type(propval_b)) \
                and isinstance(propval_a, COMPARABLE):
            for type_union, func in COMPARE_FUNCTIONS.iteritems():
                if isinstance(propval_a, type_union):
                    for diff in func(
                        propval_a, propval_b, fs_a + [propname],
                        fs_b + [propname], options,
                    ):
                        yield diff
        elif not options.items_equal(propval_a, propval_b):
            yield DiffInfo(
                diff_type=DiffTypes.MODIFIED,
                base=fs_a + [propname],
                other=fs_b + [propname],
            )
        elif options.unchanged:
            yield DiffInfo(
                diff_type=DiffTypes.NO_CHANGE,
                base=fs_a + [propname],
                other=fs_b + [propname],
            )


def collection_generator(collection):
    """This function returns a generator which iterates over the collection,
    similar to Collection.itertuples().  Collections are viewed by this module,
    regardless of type, as a mapping from an index to the value.  For sets, the
    "index" is always None.  For dicts, it's a string, and for lists, it's an
    int.
    """
    if hasattr(collection, "itertuples"):
        return collection.itertuples()
    elif hasattr(collection, "iteritems"):
        return collection.iteritems()
    elif hasattr(collection, "__getitem__"):

        def generator():
            i = 0
            for item in collection:
                yield (i, item)
                i += 1

    else:

        def generator():
            for item in collection:
                yield (None, item)

    return generator()


# There's a lot of repetition in the following code.  It could be served by one
# function instead of 3, which would be 3 times fewer places to have bugs, but
# it would probably also be more than 3 times as difficult to debug.
def compare_collection_iter(propval_a, propval_b, fs_a=None, fs_b=None,
                            options=None):
    if fs_a is None:
        fs_a = FieldSelector(tuple())
        fs_b = FieldSelector(tuple())
    if options is None:
        options = DiffOptions()

    propvals = dict(a=propval_a, b=propval_b)
    values = dict()
    rev_keys = dict()
    compare_values = None
    id_args = options.id_args(type(propval_a).itemtype, fs_a)

    for x in "a", "b":
        propval_x = propvals[x]
        vals = values[x] = set()
        rev_key = rev_keys[x] = dict()

        seen = collections.Counter()

        for k, v in collection_generator(propval_x):
            pk = options.record_id(v, **id_args)
            if compare_values is None:
                compare_values = isinstance(pk, tuple)
            vals.add((pk, seen[pk]))
            rev_key[(pk, seen[pk])] = k
            seen[pk] += 1

    removed = values['a'] - values['b']
    added = values['b'] - values['a']

    if options.unchanged:
        unchanged = values['a'] & values['b']
        for pk, seq in unchanged:
            a_key = rev_keys['a'][pk, seq]
            b_key = rev_keys['b'][pk, seq]
            yield DiffInfo(
                diff_type=DiffTypes.NO_CHANGE,
                base=fs_a + [a_key],
                other=fs_b + [b_key],
            )

    for pk, seq in removed:
        a_key = rev_keys['a'][pk, seq]
        selector = fs_a + [a_key]
        yield DiffInfo(
            diff_type=DiffTypes.REMOVED,
            base=selector,
            other=fs_b,
        )

    for pk, seq in added:
        b_key = rev_keys['b'][pk, seq]
        selector = fs_b + [b_key]
        yield DiffInfo(
            diff_type=DiffTypes.ADDED,
            base=fs_a,
            other=selector,
        )

    if compare_values:
        for pk, seq in values['a'].intersection(values['b']):
            a_key = rev_keys['a'][pk, seq]
            b_key = rev_keys['b'][pk, seq]
            selector_a = fs_a + a_key
            selector_b = fs_b + b_key
            for diff in compare_record_iter(
                propval_a[a_key], propval_b[b_key],
                selector_a, selector_b, options,
            ):
                yield diff


def compare_list_iter(propval_a, propval_b, fs_a=None, fs_b=None,
                      options=None):
    if fs_a is None:
        fs_a = FieldSelector(tuple())
        fs_b = FieldSelector(tuple())
    if not options:
        options = DiffOptions()
    propvals = dict(a=propval_a, b=propval_b)
    values = dict()
    indices = dict()
    for x in "a", "b":
        propval_x = propvals[x]
        vals = values[x] = set()
        rev_key = indices[x] = dict()
        seen = collections.Counter()
        i = 0
        for v in propval_x:
            v = options.normalize_item(
                v, propval_a if options.duck_type else propval_x
            )
            if v is not _nothing or not options.ignore_empty_slots:
                vals.add((v, seen[v]))
                rev_key[(v, seen[v])] = i
                seen[v] += 1
            i += 1

    removed = values['a'] - values['b']
    added = values['b'] - values['a']

    if options.unchanged:
        unchanged = values['a'] & values['b']
        for v, seq in unchanged:
            a_idx = indices['a'][v, seq]
            b_idx = indices['b'][v, seq]
            yield DiffInfo(
                diff_type=DiffTypes.NO_CHANGE,
                base=fs_a + [a_idx],
                other=fs_b + [b_idx],
            )

    for v, seq in removed:
        a_key = indices['a'][v, seq]
        selector = fs_a + [a_key]
        yield DiffInfo(
            diff_type=DiffTypes.REMOVED,
            base=selector,
            other=fs_b,
        )

    for v, seq in added:
        b_key = indices['b'][v, seq]
        selector = fs_b + [b_key]
        yield DiffInfo(
            diff_type=DiffTypes.ADDED,
            base=fs_a,
            other=selector,
        )


def compare_dict_iter(propval_a, propval_b, fs_a=None, fs_b=None,
                      options=None):
    if fs_a is None:
        fs_a = FieldSelector(tuple())
        fs_b = FieldSelector(tuple())
    if not options:
        options = DiffOptions()
    propvals = dict(a=propval_a, b=propval_b)
    values = dict()
    rev_keys = dict()
    for x in "a", "b":
        propval_x = propvals[x]
        vals = values[x] = set()
        rev_key = rev_keys[x] = dict()
        seen = collections.Counter()
        for k, v in propval_x.iteritems():
            v = options.normalize_item(
                v, propval_a if options.duck_type else propval_x
            )
            if v is not _nothing or not options.ignore_empty_slots:
                vals.add((v, seen[v]))
                rev_key[(v, seen[v])] = k
                seen[v] += 1

    removed = values['a'] - values['b']
    added = values['b'] - values['a']

    if options.unchanged:
        unchanged = values['a'] & values['b']
        for v, seq in unchanged:
            a_key = rev_keys['a'][v, seq]
            b_key = rev_keys['b'][v, seq]
            yield DiffInfo(
                diff_type=DiffTypes.NO_CHANGE,
                base=fs_a + [a_key],
                other=fs_b + [b_key],
            )

    for v, seq in removed:
        a_key = rev_keys['a'][v, seq]
        selector = fs_a + [a_key]
        yield DiffInfo(
            diff_type=DiffTypes.REMOVED,
            base=selector,
            other=fs_b,
        )

    for v, seq in added:
        b_key = rev_keys['b'][v, seq]
        selector = fs_b + [b_key]
        yield DiffInfo(
            diff_type=DiffTypes.ADDED,
            base=fs_a,
            other=selector,
        )


COMPARE_FUNCTIONS = {
    list: compare_list_iter,
    tuple: compare_list_iter,
    dict: compare_dict_iter,
    Collection: compare_collection_iter,
    Record: compare_record_iter,
}


COMPARABLE = tuple(COMPARE_FUNCTIONS)


def diff_iter(base, other, options=None, **kwargs):
    """Compare a Record with another object (usually a record of the same
    type), and yield differences as :py:class:`DiffInfo` instances.

    args:
        ``base=``\ *Record*
            The 'base' object to compare against.  The enumeration in
            :py:class:`DiffTypes` is relative to this object.

        ``other=``\ *Record*\ \|\ *<object>*
            The 'other' object to compare against.  If ``duck_type`` is not
            true, then it must be of the same type as the ``base``.

        ``**kwargs``
            Specify comparison options: ``duck_type``, ``ignore_ws``, etc.  See
            :py:meth:`normalize.diff.DiffOptions.__init__` for the complete
            list.

        ``options=``\ *DiffOptions instance*
            Pass in a pre-constructed :py:class:`DiffOptions` instance.  This
            may not be specified along with ``**kwargs``.
    """
    if options is None:
        options = DiffOptions(**kwargs)
    elif len(kwargs):
        raise exc.DiffOptionsException()

    generators = []

    for type_union, func in COMPARE_FUNCTIONS.iteritems():
        if isinstance(base, type_union):
            generators.append(func(base, other, options=options))

    if len(generators) == 1:
        return generators[0]
    else:
        return chain(*generators)


class Diff(ListCollection):
    """Container for a list of differences."""
    base_type_name = SafeProperty(isa=str, extraneous=True,
                                  doc="Type name of the source object")
    other_type_name = SafeProperty(
        isa=str, extraneous=True,
        doc="Type name of the compared object; normally the same, unless "
            "the ``duck_type`` option was specified.")
    itemtype = DiffInfo

    def __str__(self):
        what = (
            "%s vs %s" % (self.base_type_name, self.other_type_name) if
            self.base_type_name != self.other_type_name else
            self.base_type_name
        )
        return "<Diff [{what}]; {n} item(s)>".format(
            n=len(self),
            what=what,
        )


def diff(base, other, **kwargs):
    """Eager version of :py:func:`diff_iter`, which takes all the same options
    and returns a :py:class:`Diff` instance."""
    return Diff(diff_iter(base, other, **kwargs),
                base_type_name=type(base).__name__,
                other_type_name=type(other).__name__)
