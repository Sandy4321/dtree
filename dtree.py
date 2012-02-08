"""
2012.1.24 CKS
Algorithms for building and using a decision tree for classification or regression.

todo:
+support regression
+support class probabilities for discrete classes
+support matching nearest node element when a new element in the query vector is encountered
-support missing elements in the query vector
-support sparse data sets
"""
from collections import defaultdict
from decimal import Decimal
from pprint import pprint
import copy
import csv
import math
import os
import cPickle as pickle
import random
import re
import unittest

VERSION = (0, 1, 4)
__version__ = '.'.join(map(str, VERSION))

# Traditional entropy.
ENTROPY1 = 'entropy1'

# Modified entropy that penalizes universally unique values.
ENTROPY2 = 'entropy2'

# Modified entropy that penalizes universally unique values
# as well as features with large numbers of values.
ENTROPY3 = 'entropy3'

DISCRETE_METRICS = [
    ENTROPY1,
    ENTROPY2,
    ENTROPY3,
]

# Simple statistical variance, the measure of how far a set of numbers
# is spread out.
VARIANCE1 = 'variance1'

# Like ENTROPY2, is the variance weighted to penalize attributes with
# universally unique values.
VARIANCE2 = 'variance2'

CONTINUOUS_METRICS = [
    VARIANCE1,
    VARIANCE2,
]

DEFAULT_DISCRETE_METRIC = ENTROPY1
DEFAULT_CONTINUOUS_METRIC = VARIANCE1

ENSEMBLE = 'ensemble'
BEST = 'best'

AGGREGATION_METHODS = [
    ENSEMBLE,
    BEST,
]

GROW_RANDOM = 'random'
GROW_AUTO_MINI_BATCH = 'auto-mini-batch'
GROW_AUTO_INCREMENTAL = 'auto-incremental'
GROWTH_METHODS = [
    GROW_RANDOM,
    GROW_AUTO_MINI_BATCH,
    GROW_AUTO_INCREMENTAL,
]

ATTR_TYPE_NOMINAL = NOM = 'nominal'
ATTR_TYPE_DISCRETE = DIS = 'discrete'
ATTR_TYPE_CONTINUOUS = CON = 'continuous'

ATTR_MODE_CLASS = CLS = 'class'

ATTR_HEADER_PATTERN = re.compile("([^,:]+):(nominal|discrete|continuous)(?::(class))?")

def mean(seq):
    return sum(seq)/float(len(seq))

def variance(seq):
    m = mean(seq)
    return sum((v-m)**2 for v in seq)/float(len(seq))

def mean_absolute_error(seq, correct):
    assert len(seq) == len(correct)
    diffs = [abs(a-b) for a,b in zip(seq,correct)]
    return sum(diffs)/float(len(diffs))

class DDist(object):
    """
    Incrementally tracks the probability distribution of discrete elements.
    """
    
    def __init__(self, seq=None):
        self.clear()
        if seq:
            for k in seq:
                self.counts[k] += 1
                self.total += 1
    
    def __cmp__(self, other):
        if not isinstance(other, type(self)):
            return NotImplemented
        return cmp(
            (frozenset(self.counts.items()), self.total),
            (frozenset(other.counts.items()), other.total)
        )
    
    def __getitem__(self, k):
        """
        Returns the probability for the given element.
        """
        cnt = 0
        if k in self.counts:
            cnt = self.counts[k]
        return cnt/float(self.total)
    
    def __hash__(self):
        return hash((frozenset(self.counts.items()), self.total))
    
    def __repr__(self):
        s = []
        for k,prob in self.probs:
            s.append("%s=%s" % (k,prob))
        return "<%s %s>" % (type(self).__name__, ', '.join(s))
    
    def add(self, k, count=1):
        """
        Increments the count for the given element.
        """
        self.counts[k] += count
        self.total += count
    
    @property
    def best(self):
        """
        Returns the element with the highest probability.
        """
        b = (-1e999999, None)
        for k,c in self.counts.iteritems():
            b = max(b, (c,k))
        return b[1]
    
    @property
    def best_prob(self):
        probs = self.probs
        if not probs:
            return
        best = -1e999999
        for _, prob in probs:
            best = max(best, prob)
        return best
    
    def clear(self):
        self.counts = defaultdict(int)
        self.total = 0
    
    def copy(self):
        return copy.deepcopy(self)
    
    @property
    def count(self):
        """
        The total number of samples forming this distribution.
        """
        return self.total
    
    def keys(self):
        return self.counts.keys()
    
    @property
    def probs(self):
        """
        Returns a list of probabilities for all elements in the form
        [(value1,prob1),(value2,prob2),...].
        """
        return [
            (k, self.counts[k]/float(self.total))
            for k in self.counts.iterkeys()
        ]
    
    def update(self, dist):
        """
        Adds the given distribution's counts to the current distribution.
        """
        assert isinstance(dist, DDist)
        for k,c in dist.counts.iteritems():
            self.counts[k] += c
        self.total += dist.total

class CDist(object):
    """
    Incrementally tracks the probability distribution of continuous numbers.
    """
    
    def __init__(self, seq=None):
        self.clear()
        if seq:
            for n in seq:
                self += n
    
    def clear(self):
        self.mean_sum = 0
        self.mean_count = 0
        self.last_variance = 0
    
    def copy(self):
        return copy.deepcopy(self)
    
    def __repr__(self):
        return "<%s mean=%s variance=%s>" \
            % (type(self).__name__, self.mean, self.variance)
    
    def __iadd__(self, value):
        last_mean = self.mean
        self.mean_sum += value
        self.mean_count += 1
        if last_mean is not None:
            self.last_variance = self.last_variance \
                + (value  - last_mean)*(value - self.mean)
        return self
    
    @property
    def count(self):
        """
        The total number of samples forming this distribution.
        """
        return self.mean_count
        
    @property
    def mean(self):
        if self.mean_count:
            return self.mean_sum/float(self.mean_count)
    
    @property
    def variance(self):
        if self.mean_count:
            return self.last_variance/float(self.mean_count)


def entropy(data, class_attr=None, method=DEFAULT_DISCRETE_METRIC):
    """
    Calculates the entropy of the attribute attr in given data set data.
    
    Parameters:
    data<dict|list> :=
        if dict, treated as value counts of the given attribute name
        if list, treated as a raw list from which the value counts will be generated
    attr<string> := the name of the class attribute
    """
    assert (class_attr is None and isinstance(data,dict)) \
        or (class_attr is not None and isinstance(data,list))
    if isinstance(data, dict):
        counts = data
    else:
        counts = defaultdict(float) # {attr:count}
        for record in data:
            # Note: A missing attribute is treated like an attribute with a value
            # of None, representing the attribute is "irrelevant".
            counts[record.get(class_attr)] += 1.0
    len_data = sum(cnt for _,cnt in counts.iteritems())
    n = max(2, len(counts))
    total = float(sum(counts.values()))
    assert total, "There must be at least one non-zero count."
    try:
        #return -sum((count/total)*math.log(count/total,n) for count in counts)
        if method == ENTROPY1:
            return -sum((count/len_data)*math.log(count/len_data,n)
                for count in counts.itervalues())
        elif method == ENTROPY2:
            return -sum((count/len_data)*math.log(count/len_data,n)
                for count in counts.itervalues()) - ((len(counts)-1)/float(total))
        elif method == ENTROPY3:
            return -sum((count/len_data)*math.log(count/len_data,n)
                for count in counts.itervalues()) - 100*((len(counts)-1)/float(total))
        else:
            raise Exception, "Unknown entropy method %s." % method
    except:
        print 'Error:',counts
        raise

def entropy_variance(data, class_attr=None,
    method=DEFAULT_CONTINUOUS_METRIC):
    """
    Calculates the variance fo a continuous class attribute, to be used as an
    entropy metric.
    """
    assert method in CONTINUOUS_METRICS, "Unknown entropy variance metric: %s" % (method,)
    assert (class_attr is None and isinstance(data,dict)) \
        or (class_attr is not None and isinstance(data,list))
    if isinstance(data, dict):
        lst = data
    else:
        lst = [record.get(class_attr) for record in data]
    return variance(lst)

def gain(data, attr, class_attr,
    method=DEFAULT_DISCRETE_METRIC,
    only_sub=0, prefer_fewer_values=False, entropy_func=None):
    """
    Calculates the information gain (reduction in entropy) that would
    result by splitting the data on the chosen attribute (attr).
    
    Parameters:
    
    prefer_fewer_values := Weights the gain by the count of the attribute's
        unique values. If multiple attributes have the same gain, but one has
        slightly fewer attributes, this will cause the one with fewer
        attributes to be preferred.
    """
    entropy_func = entropy_func or entropy
    val_freq       = defaultdict(float)
    subset_entropy = 0.0

    # Calculate the frequency of each of the values in the target attribute
    for record in data:
        val_freq[record.get(attr)] += 1.0

    # Calculate the sum of the entropy for each subset of records weighted
    # by their probability of occuring in the training set.
    for val in val_freq.keys():
        val_prob        = val_freq[val] / sum(val_freq.values())
        data_subset     = [record for record in data if record.get(attr) == val]
        e = entropy_func(data_subset, class_attr, method=method)
        subset_entropy += val_prob * e
        
    if only_sub:
        return subset_entropy

    # Subtract the entropy of the chosen attribute from the entropy of the
    # whole data set with respect to the target attribute (and return it)
    main_entropy = entropy_func(data, class_attr, method=method)
    
    # Prefer gains on attributes with fewer values.
    if prefer_fewer_values:
#        n = len(val_freq)
#        w = (n+1)/float(n)/2
        #return (main_entropy - subset_entropy)*w
        return ((main_entropy - subset_entropy), 1./len(val_freq))
    else:
        return (main_entropy - subset_entropy)

def gain_variance(*args, **kwargs):
    """
    Calculates information gain using variance as the comparison metric.
    """
    return gain(entropy_func=entropy_variance, *args, **kwargs)

def majority_value(data, class_attr):
    """
    Creates a list of all values in the target attribute for each record
    in the data list object, and returns the value that appears in this list
    the most frequently.
    """
    if is_continuous(data[0][class_attr]):
        return CDist(seq=[record[class_attr] for record in data])
    else:
        return most_frequent([record[class_attr] for record in data])

def most_frequent(lst):
    """
    Returns the item that appears most frequently in the given list.
    """
    lst = lst[:]
    highest_freq = 0
    most_freq = None

    for val in unique(lst):
        if lst.count(val) > highest_freq:
            most_freq = val
            highest_freq = lst.count(val)
            
    return most_freq

def unique(lst):
    """
    Returns a list made up of the unique values found in lst.  i.e., it
    removes the redundant values in lst.
    """
    lst = lst[:]
    unique_lst = []

    # Cycle through the list and add each value to the unique list only once.
    for item in lst:
        if unique_lst.count(item) <= 0:
            unique_lst.append(item)
            
    # Return the list with all redundant values removed.
    return unique_lst

def get_values(data, attr):
    """
    Creates a list of values in the chosen attribut for each record in data,
    prunes out all of the redundant values, and return the list.  
    """
    return unique([record[attr] for record in data])

def choose_attribute(data, attributes, class_attr, fitness, method):
    """
    Cycles through all the attributes and returns the attribute with the
    highest information gain (or lowest entropy).
    """
    best = (-1e999999, None)
    for attr in attributes:
        if attr == class_attr:
            continue
        gain = fitness(data, attr, class_attr, method=method)
        best = max(best, (gain, attr))
    return best[1]

def is_continuous(v):
    return isinstance(v, (float, Decimal))

def create_decision_tree(data, attributes, class_attr, fitness_func, wrapper, split_attr=None, split_val=None):
    """
    Returns a new decision tree based on the examples given.
    """
#    print 'fitness_func1:',fitness_func
    node = None
    data = list(data) if isinstance(data, Data) else data
    if wrapper.is_continuous_class:
        stop_value = CDist(seq=[r[class_attr] for r in data])
        # For a continuous class case, stop if all the remaining records have
        # a variance below the given threshold.
        stop = wrapper.leaf_threshold is not None \
            and stop_value.variance <= wrapper.leaf_threshold
    else:
        stop_value = DDist(seq=[r[class_attr] for r in data])
        # For a discrete class, stop if all remaining records have the same
        # classification.
        stop = len(stop_value.counts) <= 1

    if not data or (len(attributes) - 1) <= 0:
        # If the dataset is empty or the attributes list is empty, return the
        # default value. When checking the attributes list for emptiness, we
        # need to subtract 1 to account for the target attribute.
        if wrapper:
            wrapper.leaf_count += 1
        return stop_value
    elif stop:
        # If all the records in the dataset have the same classification,
        # return that classification.
        if wrapper:
            wrapper.leaf_count += 1
        return stop_value
    else:
        # Choose the next best attribute to best classify our data
        best = choose_attribute(
            data,
            attributes,
            class_attr,
            fitness_func,
            method=wrapper.metric)

        # Create a new decision tree/node with the best attribute and an empty
        # dictionary object--we'll fill that up next.
#        tree = {best:{}}
        node = Node(tree=wrapper, attr_name=best)
        node.n += len(data)

        # Create a new decision tree/sub-node for each of the values in the
        # best attribute field
        for val in get_values(data, best):
            # Create a subtree for the current value under the "best" field
            subtree = create_decision_tree(
                [r for r in data if r[best] == val],
                [attr for attr in attributes if attr != best],
                class_attr,
                fitness_func,
                split_attr=best,
                split_val=val,
                wrapper=wrapper)

            # Add the new subtree to the empty dictionary object in our new
            # tree/node we just created.
            if isinstance(subtree, Node):
                node._branches[val] = subtree
            elif isinstance(subtree, (CDist, DDist)):
                node.set_leaf_dist(attr_value=val, dist=subtree)
            else:
                raise Exception, "Unknown subtree type: %s" % (type(subtree),)

    return node

class Data(object):
    """
    Parses, validates and iterates over tabular data in a file
    or an generic iterator.
    
    This does not store the actual data rows. It only stores the row schema.
    """
    
    def __init__(self, inp, order=None, types=None, modes=None):
        
        self.header_types = types or {} # {attr_name:type}
        self.header_modes = modes or {} # {attr_name:mode}
        if isinstance(order, basestring):
            order = order.split(',')
        self.header_order = order or [] # [attr_name,...]
        
        self.filename = None
        self.data = None
        if isinstance(inp, basestring):
            filename = inp
            assert os.path.isfile(filename), \
                "File \"%s\" does not exist." % filename
            self.filename = filename
        else:
            assert self.header_types, "No attribute types specified."
            assert self.header_modes, "No attribute modes specified."
            #assert self.header_order, "No attribute order specified."
            self.data = inp
        
        self._class_attr_name = None
        if self.header_modes:
            for k,v in self.header_modes.iteritems():
                if v != CLS:
                    continue
                self._class_attr_name = k
                break
            assert self._class_attr_name, "No class attribute specified."
                
    def __len__(self):
        if self.filename:
            return max(0, open(self.filename).read().strip().count('\n'))
        elif hasattr(self.data, '__len__'):
            return len(self.data)

    @property
    def class_attribute_name(self):
        return self._class_attr_name

    @property
    def attribute_names(self):
        self._read_header()
        return [
            n for n in self.header_types.iterkeys()
            if n != self._class_attr_name
        ]

    def get_attribute_type(self, name):
        if not self.header_types:
            self._read_header()
        return self.header_types[name]

    @property
    def is_continuous_class(self):
        self._read_header()
        return self.get_attribute_type(self._class_attr_name) \
            == ATTR_TYPE_CONTINUOUS

    def is_valid(self, name, value):
        """
        Returns true if the given value matches the type for the given name
        according to the schema.
        Returns false otherwise.
        """
        if name not in self.header_types:
            return False
        t = self.header_types[name]
        if t == ATTR_TYPE_DISCRETE:
            return isinstance(value, int)
        elif t == ATTR_TYPE_CONTINUOUS:
            return isinstance(value, (float, Decimal))
        return True

    def _read_header(self):
        """
        When a CSV file is given, extracts header information the file.
        Otherwise, this header data must be explicitly given when the object
        is instantiated.
        """
        if not self.filename or self.header_types:
            return
        rows = csv.reader(open(self.filename))
        header = rows.next()
        self.header_types = {} # {attr_name:type}
        self._class_attr_name = None
        self.header_order = [] # [attr_name,...]
        for el in header:
            matches = ATTR_HEADER_PATTERN.findall(el)
            assert matches, "Invalid header element: %s" % (el,)
            el_name,el_type,el_mode = matches[0]
            el_name = el_name.strip()
            self.header_order.append(el_name)
            self.header_types[el_name] = el_type
            if el_mode == ATTR_MODE_CLASS:
                assert self._class_attr_name is None, \
                    "Multiple class attributes are not supported."
                self._class_attr_name = el_name
            else:
                assert self.header_types[el_name] != ATTR_TYPE_CONTINUOUS, \
                    "Non-class continuous attributes are not supported."
        assert self._class_attr_name, "A class attribute must be specified."

    def validate_row(self, row):
        """
        Ensure each element in the row matches the schema.
        """
        clean_row = {}
        if isinstance(row, (tuple, list)):
            assert self.header_order, "No attribute order specified."
            assert len(row) == len(self.header_order), \
                "Row length does not match header length."
            itr = zip(self.header_order, row)
        else:
            assert isinstance(row, dict)
            itr = row.iteritems()
        for el_name, el_value in itr:
            if self.header_types[el_name] == ATTR_TYPE_DISCRETE:
                clean_row[el_name] = int(el_value)
            elif self.header_types[el_name] == ATTR_TYPE_CONTINUOUS:
                clean_row[el_name] = float(el_value)
            else:
                clean_row[el_name] = el_value
        return clean_row

    def _get_iterator(self):
        if self.filename:
            self._read_header()
            itr = csv.reader(open(self.filename))
            itr.next() # Skip header.
            return itr
        return self.data

    def __iter__(self):
        for row in self._get_iterator():
            if not row:
                continue
            yield self.validate_row(row)

USE_NEAREST = 'use_nearest'
MISSING_VALUE_POLICIES = set([
    USE_NEAREST,
])

def _get_dd_int():
    return defaultdict(int)

def _get_dd_dd_int():
    return defaultdict(_get_dd_int)

def _get_dd_cdist():
    return defaultdict(CDist)

class NodeNotReadyToPredict(Exception):
    pass

class Node(object):
    """
    Represents a specific split or branch in the tree.
    """
    
    def __init__(self, tree, attr_name=None):
        
        # The number of samples this node has been trained on.
        self.n = 0
        
        # A reference to the container tree instance.
        self._tree = tree
        
        # The splitting attribute at this node.
        self.attr_name = attr_name
        
        #### Discrete values.
        
        # Counts of each observed attribute value, used to calculate an
        # attribute value's probability.
        # {attr_name:{attr_value:count}}
        self._attr_value_counts = defaultdict(_get_dd_int)
        # {attr_name:total}
        self._attr_value_count_totals = defaultdict(int)
        
        # Counts of each observed class value and attribute value in
        # combination, used to calculate an attribute value's entropy.
        # {attr_name:{attr_value:{class_value:count}}}
        self._attr_class_value_counts = defaultdict(_get_dd_dd_int)
        
        #### Continuous values.
        
        # Counts of each observed class value, used to calculate a class
        # value's probability.
        # {class_value:count}
        self._class_ddist = DDist()
        
        # {attr_name:{attr_value:CDist(variance)}}
        self._attr_value_cdist = defaultdict(_get_dd_cdist)
        self._class_cdist = CDist()
        
        self._branches = {} # {v:Node}
    
    def __getitem__(self, attr_name):
        assert attr_name == self.attr_name
        branches = self._branches.copy()
        for value in self.get_values(attr_name):
            if value in branches:
                continue
            elif self.tree.data.is_continuous_class:
                branches[value] = self._attr_value_cdist[self.attr_name][value].copy()
            else:
                branches[value] = self.get_value_ddist(self.attr_name, value)
        return branches

    def _get_attribute_value_for_node(self, record):
        """
        Gets the closest value for the current node's attribute matching the
        given record.
        """
        
        # Abort if this node has not get split on an attribute. 
        if self.attr_name is None:
            return
        
        # Otherwise, lookup the attribute value for this node in the
        # given record.
        attr = self.attr_name
        attr_value = record[attr]
        attr_values = self.get_values(attr)
        if attr_value in attr_values:
            return attr_value
        else:
            # The value of the attribute in the given record does not directly
            # map to any previously known values, so apply a missing value
            # policy.
            policy = self.tree.missing_value_policy.get(attr)
            assert policy, \
                ("No missing value policy specified for attribute %s.") \
                % (attr,)
            if policy == USE_NEAREST:
                # Use the value that the tree has seen that's also has the
                # smallest Euclidean distance to the actual value.
                assert self.tree.data.header_types[attr] \
                    in (ATTR_TYPE_DISCRETE, ATTR_TYPE_CONTINUOUS), \
                    "The use-nearest policy is invalid for nominal types."
                nearest = (1e999999, None)
                for _value in attr_values:
                    nearest = min(
                        nearest,
                        (abs(_value - attr_value), _value))
                _,nearest_value = nearest
                return nearest_value
            else:
                raise Exception, "Unknown missing value policy: %s" % (policy,)

    @property
    def attributes(self):
#        if self._tree.data.is_continuous_class:
#            return self._attr_value_cdist.iterkeys()
#        else:
        return self._attr_value_counts.iterkeys()
    
    def get_values(self, attr_name):
        """
        Retrieves the unique set of values seen for the given attribute
        at this node.
        """
        ret = set(self._attr_value_cdist[attr_name].keys() \
            + self._attr_value_counts[attr_name].keys() \
            + self._branches.keys())
        return ret
    
    @property
    def is_continuous_class(self):
        return self._tree.is_continuous_class

    def get_best_splitting_attr(self):
        """
        Returns the name of the attribute with the highest gain.
        """
        best = (-1e999999, None)
        for attr in self.attributes:
            best = max(best, (self.get_gain(attr), attr))
        best_gain,best_attr = best
        return best_attr

    def get_entropy(self, attr_name=None, attr_value=None):
        """
        Calculates the entropy of a specific attribute/value combination.
        """
        is_con = self.tree.data.is_continuous_class
        if is_con:
            if attr_name is None:
                # Calculate variance of class attribute.
                var = self._class_cdist.variance
            else:
                # Calculate variance of the given attribute.
                var = self._attr_value_cdist[attr_name][attr_value].variance
            if self.tree.metric == VARIANCE1 or attr_name is None:
                return var
            elif self.tree.metric == VARIANCE2:
                unique_value_count = len(self._attr_value_counts[attr_name])
                attr_total = float(self._attr_value_count_totals[attr_name])
                return var*(unique_value_count/attr_total)
        else:
            if attr_name is None:
                # The total number of times this attr/value pair has been seen.
                total = float(self._class_ddist.total)
                # The total number of times each class value has been seen for
                # this attr/value pair.
                counts = self._class_ddist.counts
                # The total number of unique values seen for this attribute.
                unique_value_count = len(self._class_ddist.counts)
                # The total number of times this attribute has been seen.
                attr_total = total
            else:
                total = float(self._attr_value_counts[attr_name][attr_value])
                counts = self._attr_class_value_counts[attr_name][attr_value]
                unique_value_count = len(self._attr_value_counts[attr_name])
                attr_total = float(self._attr_value_count_totals[attr_name])
            assert total, "There must be at least one non-zero count."
            
            n = max(2, len(counts))
            if self._tree.metric == ENTROPY1:
                # Traditional entropy.
                return -sum(
                    (count/total)*math.log(count/total,n)
                    for count in counts.itervalues()
                )
            elif self._tree.metric == ENTROPY2:
                # Modified entropy that down-weights universally unique values.
                # e.g. If the number of unique attribute values equals the total
                # count of the attribute, then it has the maximum amount of unique
                # values.
                return -sum(
                    (count/total)*math.log(count/total,n)
                    for count in counts.itervalues()
                #) - ((len(counts)-1)/float(total))
                ) + (unique_value_count/attr_total)
            elif self._tree.metric == ENTROPY3:
                # Modified entropy that down-weights universally unique values
                # as well as features with large numbers of values.
                return -sum(
                    (count/total)*math.log(count/total,n)
                    for count in counts.itervalues()
                #) - 100*((len(counts)-1)/float(total))
                ) + 100*(unique_value_count/attr_total)
        
    def get_gain(self, attr_name):
        """
        Calculates the information gain from splitting on the given attribute.
        """
        subset_entropy = 0.0
        for value in self._attr_value_counts[attr_name].iterkeys():
            value_prob = self.get_value_prob(attr_name, value)
            e = self.get_entropy(attr_name, value)
            subset_entropy += value_prob * e
        return (self.main_entropy - subset_entropy)

    def get_value_ddist(self, attr_name, attr_value):
        """
        Returns the class value probability distribution of the given
        attribute value.
        """
        assert not self.tree.data.is_continuous_class, \
            "Discrete distributions are only maintained for " + \
            "discrete class types."
        ddist = DDist()
        cls_counts = self._attr_class_value_counts[attr_name][attr_value]
        for cls_value,cls_count in cls_counts.iteritems():
            ddist.add(cls_value, count=cls_count)
        return ddist
    
    def get_value_prob(self, attr_name, value):
        """
        Returns the value probability of the given attribute at this node.
        """
        if attr_name not in self._attr_value_count_totals:
            return
        n = self._attr_value_counts[attr_name][value]
        d = self._attr_value_count_totals[attr_name]
        return n/float(d)

    @property
    def main_entropy(self):
        """
        Calculates the overall entropy of the class attribute.
        """
        return self.get_entropy()
    
    def predict(self, record, depth=0):
        """
        Returns the estimated value of the class attribute for the given
        record.
        """
        
        # Check if we're ready to predict.
        if not self.ready_to_predict:
            raise NodeNotReadyToPredict
        
        # Lookup attribute value.
        attr_value = self._get_attribute_value_for_node(record)
        
        # Propagate decision to leaf node.
        if self.attr_name:
            if attr_value in self._branches:
                try:
                    return self._branches[attr_value].predict(record, depth=depth+1)
                except NodeNotReadyToPredict:
                    #TODO:allow re-raise if user doesn't want an intermediate prediction?
                    pass
                
        # Otherwise make decision at current node.
        if self.attr_name:
            if self._tree.data.is_continuous_class:
                return self._attr_value_cdist[self.attr_name][attr_value].copy()
            else:
#                return self._class_ddist.copy()
                return self.get_value_ddist(self.attr_name, attr_value)
        elif self._tree.data.is_continuous_class:
            # Make decision at current node, which may be a true leaf node
            # or an incomplete branch in a tree currently being built.
            assert self._class_cdist is not None
            return self._class_cdist.copy()
        else:
            return self._class_ddist.copy()

    @property
    def ready_to_predict(self):
        return self.n > 0

    @property
    def ready_to_split(self):
        """
        Returns true if this node is ready to branch off additional nodes.
        Returns false otherwise.
        """
        # Never split if we're a leaf that predicts adequately.
        threshold = self._tree.leaf_threshold
        if self._tree.data.is_continuous_class:
            var = self._class_cdist.variance
            if var is not None and threshold is not None \
            and var <= threshold:
                return False
        else:
            best_prob = self._class_ddist.best_prob
            if best_prob is not None and threshold is not None \
            and best_prob >= threshold:
                return False
            
        return self._tree.auto_grow \
            and not self.attr_name \
            and self.n >= self._tree.splitting_n

    def set_leaf_dist(self, attr_value, dist):
        """
        Sets the probability distribution at a leaf node.
        """
        assert self.attr_name
        assert self.tree.data.is_valid(self.attr_name, attr_value), \
            "Value %s is invalid for attribute %s." \
                % (attr_value, self.attr_name)
        if self.is_continuous_class:
            assert isinstance(dist, CDist)
            assert self.attr_name
            self._attr_value_cdist[self.attr_name][attr_value] = dist.copy()
#            self.n += dist.count
        else:
            assert isinstance(dist, DDist)
            # {attr_name:{attr_value:count}}
            self._attr_value_counts[self.attr_name][attr_value] += 1
            # {attr_name:total}
            self._attr_value_count_totals[self.attr_name] += 1
            # {attr_name:{attr_value:{class_value:count}}}
            for cls_value,cls_count in dist.counts.iteritems():
                self._attr_class_value_counts[self.attr_name][attr_value] \
                    [cls_value] += cls_count
    
    def to_dict(self):
        if self.attr_name:
            # Show a value's branch, whether it's a leaf or another node.
            ret = {self.attr_name:{}} # {attr_name:{attr_value:dist or node}}
            values = self.get_values(self.attr_name)
            for attr_value in values:
                if attr_value in self._branches:
                    ret[self.attr_name][attr_value] = self._branches[attr_value].to_dict()
                elif self._tree.data.is_continuous_class:
                    ret[self.attr_name][attr_value] = self._attr_value_cdist[self.attr_name][attr_value].copy()
                else:
                    ret[self.attr_name][attr_value] = self.get_value_ddist(self.attr_name, attr_value)
            return ret
        elif self.tree.data.is_continuous_class:
            # Otherwise we're at a continuous leaf node.
            return self._class_cdist.copy()
        else:
            # Or a discrete leaf node.
            return self._class_ddist.copy()

    @property
    def tree(self):
        return self._tree

    def update(self, record):
        """
        Incrementally update the statistics at this node.
        """
        self.n += 1
        class_attr = self.tree.data.class_attribute_name
        class_value = record[class_attr]
        
        # Update class statistics.
        is_con = self.tree.data.is_continuous_class
        if is_con:
            # For a continuous class.
            self._class_cdist += class_value
        else:
            # For a discrete class.
            self._class_ddist.add(class_value)
        
        # Update attribute statistics.
        for an,av in record.iteritems():
            if an == class_attr:
                continue
            self._attr_value_counts[an][av] += 1
            self._attr_value_count_totals[an] += 1
            if is_con:
                self._attr_value_cdist[an][av] += class_value
            else:
                self._attr_class_value_counts[an][av][class_value] += 1
        
        # Decide if branch should split on an attribute.
        if self.ready_to_split:
            self.attr_name = self.get_best_splitting_attr()
#            print 'splitting on',self.attr_name
            self.tree.leaf_count -= 1
            for av in self._attr_value_counts[self.attr_name]:
                self._branches[av] = Node(tree=self.tree)
                self.tree.leaf_count += 1
            
        # If we've split, then propagate the update to appropriate sub-branch.
        if self.attr_name:
            key = record[self.attr_name]
            del record[self.attr_name]
            self._branches[key].update(record)

class Tree(object):
    """
    Represents a single grown or built decision tree.
    """
    #TODO:merge with DTree
    
    def __init__(self, data, **kwargs):
        assert isinstance(data, Data)
        self._data = data
        
        # Root splitting node.
        # This can be traversed via [name1][value1][name2][value2]...
        self._tree = Node(self)
        
        # The mean absolute error.
        self.mae = CDist()
        
        # Set the metric used to calculate the information gain
        # after an attribute split.
        if self.data.is_continuous_class:
            self.metric = kwargs.get('metric', DEFAULT_CONTINUOUS_METRIC)
            assert self.metric in CONTINUOUS_METRICS
        else:
            self.metric = kwargs.get('metric', DEFAULT_DISCRETE_METRIC)
            assert self.metric in DISCRETE_METRICS
            
        # Set metric to splitting nodes after a sample threshold has been met.
        self.splitting_n = kwargs.get('splitting_n', 100)
        
        # Declare the policy for handling missing values for each attribute.
        self.missing_value_policy = {}
        
        # Allow the tree to automatically grow and split after an update().
        self.auto_grow = kwargs.get('auto_grow', False)
        
        # Determine the threshold at which further splitting is unnecessary
        # if enough accuracy has been achieved.
        if self.data.is_continuous_class:
            # Zero variance is the default continuous stopping criteria.
            self.leaf_threshold = kwargs.get('leaf_threshold', 0.0)
        else:
            # A 100% probability is the default discrete stopping criteria.
            self.leaf_threshold = kwargs.get('leaf_threshold', 1.0)
            
        # The total number of leaf nodes.
        self.leaf_count = 0
    
    def __getitem__(self, attr_name):
        return self.tree[attr_name]

    @classmethod
    def build(cls, data, *args, **kwargs):
        """
        Constructs a classification or regression tree in a single batch by
        analyzing the given data.
        """
        assert isinstance(data, Data)
        if data.is_continuous_class:
            fitness_func = gain_variance
        else:
            fitness_func = gain
#        print 'fitness_func:',fitness_func
        
        t = cls(data=data, *args, **kwargs)
        t._data = data
        t._tree = create_decision_tree(
            data=data,
            attributes=data.attribute_names,
            class_attr=data.class_attribute_name,
            fitness_func=fitness_func,
            wrapper=t,
        )
        return t
    
    @property
    def data(self):
        return self._data
    
    @property
    def is_continuous_class(self):
        return self.data.is_continuous_class
    
    @classmethod
    def load(cls, fn):
        tree = pickle.load(open(fn))
        assert isinstance(tree, cls), "Invalid pickle."
        return tree

    def predict(self, record):
        record = record.copy()
        return self._tree.predict(record)
    
    def save(self, fn):
        pickle.dump(self, open(fn,'w'))
    
    def set_missing_value_policy(self, policy, target_attr_name=None):
        """
        Sets the behavior for one or all attributes to use when traversing the
        tree using a query vector and it encounters a branch that does not
        exist.
        """
        assert policy in MISSING_VALUE_POLICIES, \
            "Unknown policy: %s" % (policy,)
        for attr_name in self.data.attribute_names:
            if target_attr_name is not None and target_attr_name != attr_name:
                continue
            self.missing_value_policy[attr_name] = policy

    def test(self, data):
        """
        Iterates over the data, classifying or regressing each element and then
        finally returns the classification accuracy or mean-absolute-error.
        """
#        assert data.header_types == self._data.header_types, \
#            "Test data schema does not match the tree's schema."
        is_continuous = self._data.is_continuous_class
        agg = CDist()
        for record in data:
#            print 'record:',record
            actual_value = self.predict(record)
#            print 'actual_value:',actual_value
            expected_value = record[self._data.class_attribute_name]
            if is_continuous:
                assert isinstance(actual_value, CDist)
                actual_value = actual_value.mean
                agg += abs(actual_value - expected_value)
            else:
                assert isinstance(actual_value, DDist)
                agg += actual_value.best == expected_value
        return agg
    
    def to_dict(self):
        return self._tree.to_dict()
    
    @property
    def tree(self):
        return self._tree
    
    def update(self, record):
        """
        Incrementally updates the tree with the given sample record.
        """
        assert self.data.class_attribute_name in record, \
            "The class attribute must be present in the record."
        record = record.copy()
        self.tree.update(record)

def _get_defaultdict_cdist():
    return defaultdict(CDist)

class Forest(object):
    
    def __init__(self, class_attr, **kwargs):
        
        self.class_attr = class_attr
        self.class_attr_min = kwargs.get('class_attr_min', None)
        self.class_attr_max = kwargs.get('class_attr_max', None)
        self.attribute_values = defaultdict(DDist) # {attr_name:DDist(k=prob)}
        # Average class value per attribute value.
        # {attr_name:{attr_value:cdist}}
        self.attribute_value_cdists = defaultdict(_get_defaultdict_cdist)
        
        self.trees = []
        self.growth_method = kwargs.get('growth_method', GROW_RANDOM)
        
        # Mini-batch training parameters.
        self.mini_batch_size = kwargs.get('mini_batch_size', 5)
        self.mini_batch_sampling = kwargs.get('mini_batch_sampling', 0.5)
        self.aggregation_method = kwargs.get('aggregation_method', ENSEMBLE)
        self._in_bag_samples = []
        self._out_of_bag_samples = []
        
        assert self.aggregation_method in AGGREGATION_METHODS, \
            "Method %s is not supported." % (self.aggregation_method,)
    
    def _grow_tree(self, attr_names, top=True):
        
        attr_names = list(attr_names)
        key = tuple(attr_names)
        attr_name = attr_names.pop(0)
        tree = {attr_name:{}}
        for attr_value in self.attribute_values[attr_name].counts.iterkeys():
            if attr_names:
                # Construct a branch.
                sub_tree = self._grow_tree(attr_names, top=False)
                tree[attr_name][attr_value] = sub_tree
            else:
                # Construct a leaf.
                seed_cdist = self.attribute_value_cdists[attr_name][attr_value]
                new_cdist = CDist()
                new_cdist += seed_cdist.mean
                tree[attr_name][attr_value] = new_cdist
        
        if top:
            t = Tree(class_attr=self.class_attr, key=key)
            t._tree = tree
            return t
        return tree
    
    def grow_randomly(self, n=10):
        if self.trees:
            return
        all_attrs = self.attribute_values.keys()
        for _ in xrange(n):
            avail_attrs = list(all_attrs)
            max_attrs = random.randint(1, len(all_attrs))
            current_attrs = []
            while len(current_attrs) < max_attrs:
                i = random.randint(0, len(avail_attrs)-1)
                random_attr = avail_attrs.pop(i)
                current_attrs.append(random_attr)
#            print current_attrs
            tree = self._grow_tree(current_attrs)
            #pprint(tree._tree, indent=4)
            self.trees.append(tree)
    
    def _get_ensemble_prediction(self, record, train=True):
        """
        Attempts to predict the value of the class attribute by aggregating
        the predictions of each tree.
        """
        
        # Get raw predictions.
        predictions = {} # {tree:raw prediction}
        total_mae = 0
        for tree in self.trees:
            predictions[tree] = prediction,tree_mae = tree.predict(record, train=train)
            total_mae += 0 if tree_mae.mean is None else tree_mae.mean

        # Weight raw predictions according to each tree's MAE.
        total = 0
        weights = []
        prediction_values = []
        for tree in predictions:
            prediction,tree_mae = predictions[tree]
            if total_mae:
                weight = 1-tree_mae.mean/total_mae
                prediction_values.append(prediction.mean)
                weights.append(weight)
                
        # Normalize weights and aggregate final prediction.
        if weights:
            weights = normalize(weights)
            total = sum(w*p for w,p in zip(weights, prediction_values))
        elif self.class_attr_min is not None and self.class_attr_max is not None:
            total = (self.class_attr_min + self.class_attr_max)/2.
        else:
            total = 0
        
        return total
    
    def _get_best_prediction(self, record, train=True):
        """
        Gets the prediction from the tree with the lowest mean absolute error.
        """
        if not self.trees:
            return
        best = (+1e999999, None)
        for tree in self.trees:
            best = min(best, (tree.mae.mean, tree))
        _,best_tree = best
        prediction,tree_mae = best_tree.predict(record, train=train)
        return prediction.mean
    
    def _add_training_sample(self, record):
        assert self.class_attr in record
        if self.aggregation_method == AUTO_MINI_BATCH:
            
            if random.random() < self.mini_batch_sampling:
                self._in_bag_samples.append(record)
            else:
                self._out_of_bag_samples.append(record)
            
            if len(self._in_bag_samples) >= self.mini_batch_size:
                self._mini_batch_train()
    
    def _mini_batch_train(self):
        """
        Uses records in the training buffer to generate a new tree and add
        to the forest.
        """
        todo
    
    def predict(self, record, train=True):
        assert method in PREDICTION_METHODS
        cattr = self.class_attr
        
        # Make prediction.
        prediction = None
        if self.trees:
            if aggregation_method == ENSEMBLE:
                prediction = self._get_ensemble_prediction(record, train=train)
            elif aggregation_method == BEST:
                prediction = self._get_best_prediction(record, train=train)
        elif self.class_attr_min is not None \
            and self.class_attr_max is not None:
            prediction = (self.class_attr_min + self.class_attr_max)/2.
        else:
            prediction = 0
            
        if train:
            self._add_training_example(record)
            
            # Update global attribute value distributions.
            for k,v in record.iteritems():
                if k == cattr:
                    continue
                self.attribute_values[k].add(v)
                if cattr in record:
                    self.attribute_value_cdists[k][v] += record[cattr]
            if cattr in record:
                self.class_attr_min = min(self.class_attr_min,
                                          record[cattr])
                self.class_attr_max = max(self.class_attr_max,
                                          record[cattr])
                
        return prediction
    
    def train(self, record):
        todo

class Test(unittest.TestCase):

    def test_stat(self):
        nums = range(1,10)
        s = CDist()
        seen = []
        for n in nums:
            seen.append(n)
            s += n
            print 'mean:',s.mean
            print 'variance:',variance(seen)
            print 'variance:',s.variance
        self.assertAlmostEqual(s.mean, mean(nums), 1)
        self.assertAlmostEqual(s.variance, variance(nums), 2)
        print 'Done.'

    def test_data(self):
        
        # Load data from a file.
        data = Data('rdata1')
        self.assertEqual(len(data), 16)
        data = list(Data('rdata1'))
        self.assertEqual(len(data), 16)
#        for row in data:
#            print row

        # Load data from memory or some other arbitrary source.
        data = """a,b,c,d,cls
1,1,1,1,a
1,1,1,2,a
1,1,2,3,a
1,1,2,4,a
1,2,3,5,a
1,2,3,6,a
1,2,4,7,a
1,2,4,8,a
2,3,5,1,b
2,3,5,2,b
2,3,6,3,b
2,3,6,4,b
2,4,7,5,b
2,4,7,6,b
2,4,8,7,b
2,4,8,8,b""".strip().split('\n')
        rows = list(csv.DictReader(data))
        self.assertEqual(len(rows), 16)
        
        rows = Data(
            #csv.DictReader(data),
            map(lambda r:r.split(','), data[1:]),
            order=['a', 'b', 'c', 'd', 'cls'],
            types=dict(a=DIS, b=DIS, c=DIS, d=DIS, cls=NOM),
            modes=dict(cls=CLS))
        self.assertEqual(len(rows), 16)
        self.assertEqual(len(list(rows)), 16)
        for row in rows:
            print row
        print 'Done.'

    def test_batch_tree(self):
        
        # If we set no leaf threshold for a continuous class
        # then there will be the same number of leaf nodes
        # as there are number of records.
        t = Tree.build(Data('rdata2'))
        self.assertEqual(type(t), Tree)
        #pprint(t._tree, indent=4)
        print "Tree:"
        pprint(t.to_dict(), indent=4)
        self.assertEqual(set(t._tree['b'].keys()), set([1,2,3,4]))
        result = t.test(Data('rdata1'))
        self.assertEqual(type(result), CDist)
        print 'MAE:',result.mean
        self.assertAlmostEqual(result.mean, 0.001368, 5)
        self.assertEqual(t.leaf_count, 16)
        
        # If we set a leaf threshold, then this will limit the number of leaf
        # nodes created, speeding up prediction, at the expense of increasing
        # the mean absolute error.
        t = Tree.build(Data('rdata2'), leaf_threshold=0.0005)
        print "Tree:"
        pprint(t.to_dict(), indent=4)
        print t._tree['b'].keys()
        self.assertEqual(t._tree.get_values('b'), set([1,2,3,4]))
        result = t.test(Data('rdata1'))
        print 'MAE:',result.mean
        self.assertAlmostEqual(result.mean, 0.00623, 5)
        self.assertEqual(t.leaf_count, 10)
        
        t = Tree.build(Data('cdata1'))
        print "Tree:"
        self.assertEqual(t['Age']['36 - 55'].attr_name, 'Marital Status')
        self.assertEqual(t['Age']['36 - 55'].get_values('Marital Status'), set(['single','married']))
        self.assertEqual(set(t['Age'].keys()), set(['< 18','18 - 35','36 - 55','> 55']))
        self.assertEqual(t['Age']['18 - 35'].best, 'won\'t buy')
        self.assertEqual(t['Age']['36 - 55']['Marital Status']['single'].best, 'will buy')
#        return
        d = t.to_dict()
        pprint(d, indent=4)
#        return
        result = t.test(Data('cdata1'))
        print 'Accuracy:',result.mean
        self.assertAlmostEqual(result.mean, 1.0, 5)
        
        t = Tree.build(Data('cdata2'))
        pprint(t.to_dict(), indent=4)
        result = t.test(Data('cdata3'))
        print 'Accuracy:',result.mean
        self.assertAlmostEqual(result.mean, 0.75, 5)
        
        # Send it a corpus that's purposefully difficult to predict.
        t = Tree.build(Data('cdata4'))
        pprint(t.to_dict(), indent=4)
        result = t.test(Data('cdata4'))
        print 'Accuracy:',result.mean
        self.assertAlmostEqual(result.mean, 0.5, 5)
        
        # Send it a case it's never seen.
        try:
            # By default, it should throw an exception because it hasn't been
            # given a policy for resolving unseen attribute value.
            t.predict(dict(a=1,b=2,c=3,d=4))
            self.assertTrue(0)
        except AssertionError:
            pass
        # But if we tell it to use the nearest value, then it should pass.
        t.set_missing_value_policy(USE_NEAREST)
        result = t.predict(dict(a=1,b=2,c=3,d=4))
        print result
        print 'Done.'

    def test_online_tree(self):
        
        rdata3 = Data('rdata3')
        rdata3_lst = list(rdata3)
        
        cdata2 = Data('cdata2')
        cdata2_lst = list(cdata2)
        
        cdata5 = Data('cdata5')
        cdata5_lst = list(cdata5)
        
        tree = Tree(cdata2, metric=ENTROPY1)
        for row in cdata2:
#            print row
            tree.update(row)
        node = tree._tree
        attr_gains = [(node.get_gain(attr_name), attr_name) for attr_name in node.attributes]
        attr_gains.sort()
#        print attr_gains
        # With traditional entropy, a b and c all evenly divide the class
        # and therefore have the same gain, even though all three
        # have different value frequencies.
        self.assertEqual(attr_gains,
            [(0.0, 'd'), (1.0, 'a'), (1.0, 'b'), (1.0, 'c')])
        
        tree = Tree(cdata2, metric=ENTROPY2)
        for row in cdata2:
#            print row
            tree.update(row)
        self.assertEqual(set(node.attributes), set(['a','b','c','d']))
        node = tree._tree
        attr_gains = [(node.get_gain(attr_name), attr_name) for attr_name in node.attributes]
        attr_gains.sort()
#        print attr_gains
        # With entropy metric 2, attributes that have fewer unique values
        # will have a slightly greater gain relative to attributes with more
        # unique values.
        self.assertEqual(attr_gains,
            [(-0.375, 'd'), (0.625, 'c'), (0.875, 'b'), (1.0, 'a')])
        
        tree = Tree(rdata3, metric=VARIANCE1)
        for row in rdata3:
#            print row
            tree.update(row)
        node = tree._tree
        self.assertEqual(set(node.attributes), set(['a','b','c','d']))
        attr_gains = [(node.get_gain(attr_name), attr_name) for attr_name in node.attributes]
        attr_gains.sort()
#        print attr_gains
        # With entropy metric 2, attributes that have fewer unique values
        # will have a slightly greater gain relative to attributes with more
        # unique values.
        self.assertEqual([v for _,v in attr_gains],
            ['d','a','b','c'])
        
        tree = Tree(rdata3, metric=VARIANCE2)
        for row in rdata3:
#            print row
            tree.update(row)
        node = tree._tree
        self.assertEqual(set(node.attributes), set(['a','b','c','d']))
        attr_gains = [(node.get_gain(attr_name), attr_name) for attr_name in node.attributes]
        attr_gains.sort()
#        print attr_gains
        # With entropy metric 2, attributes that have fewer unique values
        # will have a slightly greater gain relative to attributes with more
        # unique values.
        self.assertEqual([v for _,v in attr_gains],
            ['d','c','b','a'])
        
        #t = Tree.build(Data('cdata2'))
        #pprint(t._tree, indent=4)
        
        # Incrementally grow a classification tree.
        print "-"*70
        print "Incrementally growing classification tree..."
        tree = Tree(cdata5, metric=ENTROPY2, splitting_n=17, auto_grow=True)
        for row in cdata5:
#            print row
            tree.update(row)
        acc = tree.test(cdata5)
        print 'Initial accuracy:',acc.mean
        self.assertEqual(acc.mean, 0.25)
#        print 'Current tree:'
#        pprint(tree.to_dict(), indent=4)
        # Update tree several times to give leaf nodes potential time to split.
        for _ in xrange(5):
            for row in cdata5:
                #print row
                tree.update(row)
            acc = tree.test(cdata5)
            print 'Accuracy:',acc.mean
        print 'Final tree:'
        pprint(tree.to_dict(), indent=4)
        # Confirm no more nodes have split, since the optimal split has
        # already been found and the tree is fully grown.
        self.assertEqual(tree['b'][1].ready_to_split, False)
        self.assertEqual(tree['b'][1]._branches, {})
#        for attr in tree['b'][1].attributes:
#            print attr, tree['b'][1].get_gain(attr)
        # Test accuracy of fully grown tree.
        acc = tree.test(cdata5)
        self.assertEqual(acc.mean, 1.0)
        
        # Incrementally grow a regression tree.
        print "-"*70
        print "Incrementally growing regression tree..."
        tree = Tree(rdata3, metric=VARIANCE2, splitting_n=17, auto_grow=True, leaf_threshold=0.0)
        for row in rdata3:
#            print row
            tree.update(row)
        mae = tree.test(rdata3)
        print 'Initial MAE:',mae.mean
        self.assertAlmostEqual(mae.mean, 0.4, 5)
        for _ in xrange(20):
            for row in rdata3:
                #print row
                tree.update(row)
            mae = tree.test(rdata3)
            print 'MAE:',mae.mean
        print "Final tree:"
        pprint(tree.to_dict(), indent=4)
        self.assertEqual(mae.mean, 0.0)
        print 'Done.'

if __name__ == '__main__':
    unittest.main()
    