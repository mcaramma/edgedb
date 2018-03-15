##
# Copyright (c) 2008-present MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##

"""Query scope tree implementation."""

import textwrap
import typing
import weakref

from . import pathid


class ScopeTreeNode(pathid.ScopeBranchNode):
    path_id: typing.Optional[pathid.PathId]
    """Node path id, or None for branch nodes."""

    fenced: bool
    """Whether the subtree represents a SET OF argument."""

    protect_parent: bool
    """Whether the subtree represents a scope that must not affect parents."""

    optional: bool
    """Whether this node represents an optional path."""

    children: typing.Set['ScopeTreeNode']
    """A set of child nodes."""

    namespaces: typing.Set[str]
    """A set of namespaces used by paths in this branch.

    When a path node is pulled up from this branch,
    and its namespace matches anything in `namespaces`,
    the namespace will be stripped.  This is used to
    implement "semi-detached" semantics used by
    views declared in a WITH block."""

    def __init__(self, *, path_id: typing.Optional[pathid.PathId]=None,
                 fenced: bool=False):
        self.path_id = path_id
        self.fenced = fenced
        self.protect_parent = False
        self.optional = False
        self.children = set()
        self.namespaces = set()
        self._parent = None

    def __repr__(self):
        return (f'<{type(self).__name__} '
                f'{self.path_id!r} at {id(self):0x}>')

    @property
    def name(self):
        if self.path_id is None:
            return f'FENCE' if self.fenced else f'BRANCH'
        else:
            return f'{self.path_id}{" [OPT]" if self.optional else ""}'

    @property
    def debugname(self):
        return f'{self.name} 0x{id(self):0x}'

    @property
    def ancestors(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's ancestors, including self."""
        node = self
        while node is not None:
            yield node
            node = node.parent

    @property
    def strict_ancestors(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's ancestors, not including self."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    @property
    def ancestors_and_namespaces(self) \
            -> typing.Iterator[typing.Tuple['ScopeTreeNode',
                                            typing.FrozenSet[str]]]:
        """An iterator of node's ancestors and namespaces, including self."""
        namespaces = frozenset()
        node = self
        while node is not None:
            namespaces |= node.namespaces
            yield node, namespaces
            node = node.parent

    @property
    def path_children(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's children that have path ids."""
        return filter(lambda p: p.path_id is not None, self.children)

    @property
    def descendants(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's descendants including self depth-first."""
        yield from self.strict_descendants
        yield self

    @property
    def strict_descendants(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's descendants not including self depth-first."""
        for child in tuple(self.children):
            yield from child.strict_descendants
            yield child

    @property
    def path_descendants(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's descendants that have path ids."""
        return filter(lambda p: p.path_id is not None, self.children)

    def get_all_path_nodes(self, *, include_subpaths: bool=True):  # XXX
        return list(self.path_descendants)

    @property
    def descendant_namespaces(self) -> typing.Set[str]:
        """An set of namespaces declared by descendants."""
        namespaces = set()
        for child in self.descendants:
            namespaces.update(child.namespaces)

        return namespaces

    @property
    def strict_unfenced_descendants(self) -> typing.Iterator['ScopeTreeNode']:
        """An iterator of node's unfenced descendants."""
        for child in tuple(self.children):
            if not child.fenced:
                yield from child.strict_unfenced_descendants
                yield child

    @property
    def fence(self) -> 'ScopeTreeNode':
        """The nearest ancestor fence (or self, if fence)."""
        if self.fenced:
            return self
        else:
            return self.parent_fence

    @property
    def parent(self) -> typing.Optional['ScopeTreeNode']:
        """The parent node."""
        if self._parent is None:
            return None
        else:
            return self._parent()

    @property
    def parent_fence(self) -> typing.Optional['ScopeTreeNode']:
        """The nearest strict ancestor fence."""
        for ancestor in self.strict_ancestors:
            if ancestor.fenced:
                return ancestor

        return None

    def find_descendant(self, path_id: pathid.PathId) \
            -> typing.Optional['ScopeTreeNode']:
        for descendant in self.strict_unfenced_descendants:
            if descendant.path_id == path_id:
                return descendant

        return None

    def attach_child(self, node: 'ScopeTreeNode') -> None:
        """Attach a child node to this node.

        This is a low-level operation, no tree validation is
        performed.  For safe tree modification, use attach_subtree()""
        """
        node._set_parent(self)

    def attach_fence(self) -> 'ScopeTreeNode':
        """Create and attach an empty fenced node."""
        fence = ScopeTreeNode(fenced=True)
        self.attach_child(fence)
        return fence

    add_fence = attach_fence  # XXX: compat

    def attach_path(self, path_id: pathid.PathId) -> None:
        """Attach a scope subtree representing *path_id*."""

        subtree = parent = ScopeTreeNode(fenced=True)
        is_lprop = False

        for prefix in reversed(list(path_id.iter_prefixes(include_ptr=True))):
            if prefix.is_ptr_path():
                is_lprop = True
                continue

            new_child = ScopeTreeNode(path_id=prefix)
            parent.attach_child(new_child)

            if not (is_lprop or prefix.is_linkprop_path()):
                parent = new_child

            is_lprop = False

        self.attach_subtree(subtree)

    add_path = attach_path   # XXX: compat

    def attach_subtree(self, node: 'ScopeTreeNode') -> None:
        """Attach a subtree to this node.

        *node* is expected to be a balanced scope tree and may be modified
        by this function.

        If *node* is not a path node (path_id is None), it is discared,
        and it's descendants are attached directly.  The tree balance is
        maintained.
        """
        if node.path_id is not None:
            # Wrap path node
            wrapper_node = ScopeTreeNode(fenced=True)
            wrapper_node.attach_child(node)
            node = wrapper_node

        dns = node.descendant_namespaces

        for descendant in node.strict_descendants:
            if descendant.path_id is not None:
                if self.find_visible(descendant.path_id, dns) is not None:
                    # This path is already present in the tree, discard.
                    descendant.destroy()
                    continue
                elif descendant.parent_fence is node:
                    # Unfenced path, unnest in ancestors.
                    unnested = self.unnest_descendants(
                        descendant.path_id.strip_namespace(dns))

                    if unnested is not None:
                        continue

            if descendant.parent is node:
                # Reached top of subtree, attach whatever is remaining
                # in the subtree.
                for pd in descendant.path_descendants:
                    to_strip = set(pd.path_id.namespace) & dns
                    pd.path_id = pd.path_id.strip_namespace(to_strip)

                self.attach_child(descendant)

    attach_branch = attach_subtree  # XXX: compat

    def remove_subtree(self, node):
        """Remove the given subtree from this node."""
        if node not in self.children:
            raise KeyError(f'{node} is not a child of {self}')

        node._set_parent(None)

    remove_child = remove_subtree  # XXX: compat

    def contain_path(self, path_id: pathid.PathId) -> None:
        pass

    def unnest_descendants(self, path_id: pathid.PathId) \
            -> typing.Optional['ScopePathNode']:
        descendants = []

        for node in self.strict_unfenced_descendants:
            if node.path_id == path_id:
                descendants.append(node)

        if descendants:
            for node in descendants[1:]:
                node.destroy()

            self.attach_child(descendants[0])

            return descendants[0]
        else:
            return None

    def destroy(self):
        """Remove this node from the tree."""
        parent = self.parent
        if parent is not None:
            parent.remove_subtree(self)

    def collapse(self):
        """Remove the node, reattaching the children to the parent."""
        parent = self.parent
        if parent is None:
            raise ValueError('cannot collapse the root node')

        if self.path_id is not None:
            subtree = ScopeTreeNode()

            for child in self.children:
                subtree.attach_child(child)
        else:
            subtree = self

        parent.attach_subtree(subtree)

    def unfence(self, node):  # XXX: compat
        node.collapse()

    def is_empty(self):
        if self.path_id is not None:
            return False
        else:
            return (
                not self.children or
                all(c.is_empty() for c in self.children)
            )

    def get_all_visible(self) -> typing.Set[pathid.PathId]:
        paths = set()

        for node in self.ancestors:
            if node.path_id:
                paths.add(node.path_id)
            else:
                for c in node.children:
                    if c.path_id:
                        paths.add(c.path_id)

        return paths

    def find_visible(
            self, path_id: pathid.PathId,
            namespaces: typing.Optional[typing.Set[str]]=None) \
            -> typing.Optional['ScopeTreeNode']:
        """Find the visible node with the given *path_id*."""
        if namespaces is None:
            namespaces = set()

        for node, ans in self.ancestors_and_namespaces:
            if _paths_equal(node.path_id, path_id, namespaces | ans):
                return node

            for child in node.children:
                if _paths_equal(child.path_id, path_id, namespaces | ans):
                    return child

        return None

    def pformat(self):
        if self.children:
            child_formats = []
            for c in self.children:
                cf = c.pformat()
                if cf:
                    child_formats.append(cf)

            if child_formats:
                child_formats = sorted(child_formats)
                children = textwrap.indent(',\n'.join(child_formats), '    ')
                return f'"{self.name}": {{\n{children}\n}}'

        if self.path_id is not None:
            return f'"{self.name}"'
        else:
            return ''

    def pdebugformat(self):
        if self.children:
            child_formats = []
            for c in self.children:
                cf = c.pdebugformat()
                if cf:
                    child_formats.append(cf)

            children = textwrap.indent(',\n'.join(child_formats), '    ')
            return f'"{self.debugname}": {{\n{children}\n}}'
        else:
            return f'"{self.debugname}"'

    def _set_parent(self, parent):
        current_parent = self.parent
        if parent is current_parent:
            return

        if current_parent is not None:
            # Make sure no other node refers to us.
            current_parent.children.remove(self)
            if str(self.path_id) == '[ns~1]@@(test::User)':
                print('REMOVING FROM', current_parent)
                import traceback
                traceback.print_stack(limit=17)

        if parent is not None:
            self._parent = weakref.ref(parent)
            parent.children.add(self)
            if str(self.path_id) == '[ns~1]@@(test::User)':
                print('ADDING TO', parent)
                import traceback
                traceback.print_stack(limit=17)
        else:
            self._parent = None


def _paths_equal(path_id_1: pathid.PathId, path_id_2: pathid.PathId,
                 namespaces: typing.Set[str]) -> bool:
    if path_id_1 is None or path_id_2 is None:
        return False

    if namespaces:
        ns1 = path_id_1.namespace
        ns2 = path_id_2.namespace

        if ns1 and ns1[-1] in namespaces:
            path_id_1 = path_id_1.replace_namespace(ns1[:-1])

        if ns2 and ns2[-1] in namespaces:
            path_id_2 = path_id_2.replace_namespace(ns2[:-1])

    return path_id_1 == path_id_2
