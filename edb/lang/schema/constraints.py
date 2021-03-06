#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import itertools

from edb.lang import edgeql
from edb.lang.edgeql import ast as qlast
from edb.lang.edgeql import errors as ql_errors

from . import delta as sd
from . import error as s_errors
from . import expr as s_expr
from . import functions as s_func
from . import inheriting
from . import name as sn
from . import named
from . import objects as so
from . import referencing


class CumulativeBoolExpr(s_expr.ExpressionText):
    @classmethod
    def merge_values(cls, ours, theirs, schema):
        if ours and theirs and ours != theirs:
            result = '({}) and ({})'.format(ours, theirs)
        elif not ours and theirs:
            result = theirs
        else:
            result = ours

        return result


class Constraint(inheriting.InheritingObject):
    _type = 'constraint'

    expr = so.Field(s_expr.ExpressionText, default=None, compcoef=0.909,
                    coerce=True)

    subjectexpr = so.Field(s_expr.ExpressionText,
                           default=None, compcoef=0.833, coerce=True)

    localfinalexpr = so.Field(CumulativeBoolExpr, default=None,
                              coerce=True, hashable=False, inheritable=False,
                              ephemeral=True)

    finalexpr = so.Field(CumulativeBoolExpr, default=None,
                         coerce=True, hashable=False, compcoef=0.909)

    subject = so.Field(so.Object, default=None, inheritable=False)

    paramnames = so.Field(so.StringList, default=None, coerce=True,
                          compcoef=0.4)

    paramtypes = so.Field(so.TypeList, default=None, coerce=True,
                          compcoef=0.857)

    # Number of the variadic parameter (+1)
    varparam = so.Field(int, default=None, compcoef=0.4)

    args = so.Field(s_expr.ExpressionList,
                    default=None, coerce=True, inheritable=False,
                    compcoef=0.875)

    errmessage = so.Field(str, default=None, compcoef=0.971)

    def generic(self):
        return self.subject is None

    def merge_localexprs(self, obj, schema):
        self.localfinalexpr = CumulativeBoolExpr.merge_values(
            self.localfinalexpr, obj.localfinalexpr, schema=schema)

    def init_derived(self, schema, source, *qualifiers,
                     as_copy, mark_derived=False, add_to_schema=False,
                     merge_bases=None, attrs=None,
                     dctx=None, **kwargs):

        if attrs is None:
            attrs = {}

        attrs['subject'] = source

        return super().init_derived(
            schema, source, *qualifiers, as_copy=as_copy,
            mark_derived=mark_derived, add_to_schema=add_to_schema,
            merge_bases=merge_bases, attrs=attrs, dctx=dctx, **kwargs)

    @classmethod
    def _dummy_subject(cls):
        from . import scalars as s_scalars

        # Point subject placeholder to a dummy pointer to make EdgeQL
        # pipeline happy.
        return s_scalars.ScalarType(name=sn.Name('std::_subject_tgt'))

    @classmethod
    def _normalize_constraint_expr(
            cls, schema, module_aliases, expr, subject, *,
            inline_anchors=False):
        from edb.lang.edgeql import parser as edgeql_parser
        from edb.lang.edgeql import utils as edgeql_utils

        if isinstance(expr, str):
            tree = edgeql_parser.parse(expr, module_aliases)
        else:
            tree = expr

        ir, edgeql_tree, _ = edgeql_utils.normalize_tree(
            tree, schema, modaliases=module_aliases,
            anchors={qlast.Subject: subject}, inline_anchors=inline_anchors)

        return edgeql_tree.result, ir.expr.expr.result

    @classmethod
    def normalize_constraint_expr(
            cls, schema, module_aliases, expr, *,
            subject=None, constraint, expr_context=None,
            enforce_boolean=False):
        from edb.lang.ir import utils as irutils

        if subject is None:
            subject = cls._dummy_subject()

        edgeql_tree, ir_result = cls._normalize_constraint_expr(
            schema, module_aliases, expr, subject)

        if enforce_boolean:
            bool_t = schema.get('std::bool')
            expr_type = irutils.infer_type(ir_result, schema)
            if not expr_type.issubclass(bool_t):
                raise s_errors.SchemaDefinitionError(
                    f'{constraint.displayname} constraint expression expected '
                    f'to return a bool value, got {expr_type.name.name!r}',
                    context=expr_context
                )

        expr = edgeql.generate_source(edgeql_tree, pretty=False)
        # XXX: check that expr has boolean result
        return expr

    @classmethod
    def process_specialized_constraint(cls, schema, constraint, params=None):
        from edb.lang.edgeql import utils as edgeql_utils
        from edb.lang.edgeql import parser as edgeql_parser

        assert constraint.subject is not None

        module_aliases = {}

        # check to make sure that the specialized constraint doesn't redefine
        # an already defined subjectexpr
        if constraint.subjectexpr is not None:
            for base in constraint.bases:
                base_se = base.get_field_value('subjectexpr')
                if base_se and base_se != constraint.subjectexpr:
                    raise s_errors.InvalidConstraintDefinitionError(
                        'subjectexpr is already defined for ' +
                        f'{constraint.name!r}')

        subject = constraint.subject
        subjectexpr = constraint.get_field_value('subjectexpr')
        if subjectexpr:
            subject, _ = cls._normalize_constraint_expr(
                schema, {}, subjectexpr, subject)

        expr = constraint.get_field_value('expr')
        if not expr:
            raise s_errors.InvalidConstraintDefinitionError(
                f'missing constraint expression in {constraint.name!r}')

        expr_ql = edgeql_parser.parse(expr, module_aliases)

        if params:
            args = params
        else:
            args = constraint.get_field_value('args')

        args_map = None
        if args:
            if constraint.varparam is not None:
                varparam = constraint.varparam
            else:
                varparam = None

            args_ql = [
                edgeql_parser.parse(arg, module_aliases) for arg in args
            ]

            args_map = edgeql_utils.index_parameters(
                args_ql, varparam=varparam)

            edgeql_utils.inline_parameters(expr_ql, args_map)

            args_map = {f'${name}': edgeql.generate_source(val, pretty=False)
                        for name, val in args_map.items()}

            constraint.errmessage = constraint.errmessage.format(
                __subject__='{__subject__}', **args_map)

            args = list(args_map.values())

        if expr == '__subject__':
            expr_context = \
                constraint.get_attribute_source_context('subjectexpr')
        else:
            expr_context = \
                constraint.get_attribute_source_context('expr')

        if subject is not constraint.subject:
            # subject has been redefined
            subject_anchor = qlast.SubExpr(
                expr=subject,
                anchors={
                    qlast.Subject: constraint.subject
                }
            )
        else:
            subject_anchor = subject

        expr_text = cls.normalize_constraint_expr(
            schema, module_aliases, expr_ql,
            subject=subject_anchor,
            constraint=constraint,
            enforce_boolean=True,
            expr_context=expr_context)

        constraint.expr = expr_text
        constraint.localfinalexpr = expr_text
        constraint.finalexpr = expr_text

        constraint.args = args or None

    def format_error_message(self):
        errmsg = self.errmessage
        subjtitle = self.subject.title

        if not subjtitle:
            try:
                subjname = self.subject.shortname
            except AttributeError:
                subjname = self.subject.name

            subjtitle = subjname.name

        formatted = errmsg.format(__subject__=subjtitle)

        return formatted

    @classmethod
    def get_root_classes(cls):
        return (
            sn.Name(module='std', name='constraint'),
        )

    @classmethod
    def get_default_base_name(self):
        return sn.Name('std::constraint')


class ConsistencySubject(referencing.ReferencingObject):
    constraints = referencing.RefDict(ref_cls=Constraint, compcoef=0.887)

    @classmethod
    def inherit_pure(cls, schema, item, source, *, dctx=None):
        item = super().inherit_pure(schema, item, source, dctx=dctx)

        if any(c.is_abstract for c in item.constraints.values()):
            # Have abstract constraints, cannot go pure inheritance,
            # must create a derived Object with materialized
            # constraints.
            generic = item.bases[0]
            item = generic.derive(schema, source=source, add_to_schema=True,
                                  merge_bases=[item], dctx=dctx)

        return item

    def begin_classref_dict_merge(self, schema, bases, attr):
        if attr == 'constraints':
            # Make sure abstract constraints from parents are mixed in
            # properly.
            constraints = set(self.constraints)
            inherited = itertools.chain.from_iterable(
                getattr(b, 'constraints', {}).values()
                for b in bases)
            constraints.update(c.shortname
                               for c in inherited if c.is_abstract)
            return constraints
        else:
            return super().begin_classref_dict_merge(schema, bases, attr)

    def finish_classref_dict_merge(self, schema, bases, attr):
        super().finish_classref_dict_merge(schema, bases, attr)

        if attr == 'constraints':
            # Materialize unmerged abstract constraints
            for cn, constraint in self.constraints.items():
                if constraint.is_abstract and cn not in self.local_constraints:
                    constraint = constraint.derive_copy(
                        schema, self, add_to_schema=True,
                        attrs=dict(is_abstract=False))

                    self.add_constraint(constraint)

    def add_constraint(self, constraint, replace=False):
        self.add_classref('constraints', constraint, replace=replace)

    def del_constraint(self, constraint_name, schema):
        self.del_classref('constraints', constraint_name, schema)

    @classmethod
    def delta_constraints(cls, set1, set2, delta, context=None):
        oldconstraints = set(set1)
        newconstraints = set(set2)

        for constraint in oldconstraints - newconstraints:
            d = set1[constraint].delta(None, reverse=True, context=context)
            delta.add(d)

        for constraint in newconstraints - oldconstraints:
            d = set2[constraint].delta(None, context=context)
            delta.add(d)

        for constraint in newconstraints & oldconstraints:
            oldconstr = set1[constraint]
            newconstr = set2[constraint]

            if newconstr.compare(oldconstr, context=context) != 1.0:
                d = newconstr.delta(oldconstr, context=context)
                delta.add(d)

    def delta_all_constraints(self, old, new, delta, context):
        oldconstraints = old.local_constraints if old else {}
        newconstraints = new.local_constraints if new else {}

        self.delta_constraints(oldconstraints, newconstraints, delta, context)


class ConsistencySubjectCommandContext:
    # context mixin
    pass


class ConsistencySubjectCommand(referencing.ReferencingObjectCommand):
    pass


class ConstraintCommandContext(sd.ObjectCommandContext):
    pass


class ConstraintCommand(
        referencing.ReferencedInheritingObjectCommand,
        schema_metaclass=Constraint, context_class=ConstraintCommandContext,
        referrer_context_class=ConsistencySubjectCommandContext):

    def add_constraint(self, constraint, parent, schema):
        parent.add_constraint(constraint)

    def delete_constraint(self, constraint_name, parent, schema):
        parent.del_constraint(constraint_name, schema)

    def _create_begin(self, schema, context):
        super()._create_begin(schema, context)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None and self.scls.finalexpr is None:
            Constraint.process_specialized_constraint(schema, self.scls)

    def _alter_begin(self, schema, context, scls):
        super()._alter_begin(schema, context, scls)

    @classmethod
    def _validate_subcommands(cls, astnode):
        # check that 'subject' and 'subjectexpr' are not set as attributes
        for command in astnode.commands:
            if cls._is_special_name(command.name):
                raise s_errors.SchemaDefinitionError(
                    f'{command.name.name} is not a valid constraint attribute',
                    context=command.context)

    @classmethod
    def _is_special_name(cls, astnode):
        # check that 'subject' and 'subjectexpr' are not set as attributes
        return (astnode.name in {'subject', 'subjectexpr'} and
                not astnode.module)


class CreateConstraint(ConstraintCommand,
                       referencing.CreateReferencedInheritingObject,
                       s_func.FunctionCommandMixin):

    astnode = [qlast.CreateConcreteConstraint, qlast.CreateConstraint]
    referenced_astnode = qlast.CreateConcreteConstraint

    @classmethod
    def _cmd_tree_from_ast(cls, astnode, context, schema):
        cmd = super()._cmd_tree_from_ast(astnode, context, schema)

        if isinstance(astnode, qlast.CreateConcreteConstraint):
            if astnode.args:
                args = []

                for arg in astnode.args:
                    arg_expr = s_expr.ExpressionText(
                        edgeql.generate_source(arg.arg, pretty=False))
                    args.append(arg_expr)

                cmd.add(
                    sd.AlterObjectProperty(
                        property='args',
                        new_value=args
                    )
                )

        elif isinstance(astnode, qlast.CreateConstraint):
            if astnode.args:
                paramnames, paramdefaults, paramtypes, paramkinds, variadic = \
                    s_func.parameters_from_ast(
                        astnode, context.modaliases, schema)

                if variadic is not None:
                    cmd.add(sd.AlterObjectProperty(
                        property='varparam',
                        new_value=variadic
                    ))

                for pname, pdefault, ptype in zip(paramnames, paramdefaults,
                                                  paramtypes):
                    if pname is not None:
                        raise ql_errors.EdgeQLError(
                            'constraints do not support named parameters',
                            context=astnode.context)

                    if pdefault is not None:
                        raise ql_errors.EdgeQLError(
                            'constraints do not support parameters '
                            'with defaults',
                            context=astnode.context)

                    if ptype is None:
                        raise ql_errors.EdgeQLError(
                            'untyped parameter', context=astnode.context)

                cmd.add(sd.AlterObjectProperty(
                    property='paramtypes',
                    new_value=paramtypes
                ))

        # 'subject' can be present in either astnode type
        if astnode.subject:
            subjectexpr = s_expr.ExpressionText(
                edgeql.generate_source(astnode.subject, pretty=False))

            cmd.add(sd.AlterObjectProperty(
                property='subjectexpr',
                new_value=subjectexpr
            ))

        cls._validate_subcommands(astnode)

        return cmd

    def _apply_field_ast(self, context, node, op):
        if op.property == 'is_derived':
            pass
        elif op.property == 'is_abstract':
            node.is_abstract = op.new_value
        elif op.property == 'subject':
            pass
        else:
            super()._apply_field_ast(context, node, op)


class RenameConstraint(ConstraintCommand, named.RenameNamedObject):
    pass


class AlterConstraint(ConstraintCommand, named.AlterNamedObject):
    astnode = [qlast.AlterConcreteConstraint, qlast.AlterConstraint]
    referenced_astnode = qlast.AlterConcreteConstraint

    @classmethod
    def _cmd_tree_from_ast(cls, astnode, context, schema):
        cmd = super()._cmd_tree_from_ast(astnode, context, schema)

        if isinstance(astnode, qlast.AlterConcreteConstraint):
            subject_ctx = context.get(ConsistencySubjectCommandContext)
            new_subject_name = None

            for op in subject_ctx.op.get_subcommands(
                    type=named.RenameNamedObject):
                new_subject_name = op.new_name

            if new_subject_name is not None:
                cmd.add(
                    sd.AlterObjectProperty(
                        property='subject',
                        new_value=so.ObjectRef(
                            classname=new_subject_name
                        )
                    )
                )

            new_name = None
            for op in cmd.get_subcommands(type=RenameConstraint):
                new_name = op.new_name

            if new_name is not None:
                cmd.add(
                    sd.AlterObjectProperty(
                        property='name',
                        new_value=new_name
                    )
                )

        cls._validate_subcommands(astnode)

        return cmd

    def _apply_field_ast(self, context, node, op):
        if op.property == 'subject':
            return
        super()._apply_field_ast(context, node, op)


class DeleteConstraint(ConstraintCommand, named.DeleteNamedObject):
    astnode = [qlast.DropConcreteConstraint, qlast.DropConstraint]
    referenced_astnode = qlast.DropConcreteConstraint
