import ast
from _ast import *
import sys
import os
import io
import contextlib
import weakref
import random

CTYPE_LITE = "LITE"
CTYPE_FULL = "FULL"

__all__ = [
    "lua_lite_compile", "lua_full_compile",
    "print_ast", "print_ast_tree",
]

class ASTTreePrinter(ast.NodeVisitor):
    @classmethod
    def unresolve(cls, value, raw=False, *, has_ast):
        if isinstance(value, AST):
            if has_ast:
                return "*"
            else:
                fname = type(value).__name__
                fargs = []
                for key, value in ast.iter_fields(value):
                    if isinstance(value, AST):
                        value = cls.unresolve(value, raw=True, has_ast=False)

                    fargs.append((key, value))
                fargs = ", ".join("%s=%s" % (key, value) for key, value in fargs)

                return '%s(%s)' % (fname, fargs)
        elif isinstance(value, list):
            newvalue = []
            for node in value:
                newvalue.append(cls.unresolve(node, raw=True, has_ast=has_ast))
            return newvalue
        elif raw:
            return value

        return repr(value)

    @classmethod
    def repr_node(cls, node, *, has_ast):
        #return dump(node, annotate_fields=False)
        result = "%s(%%s)" % (type(node).__name__,)

        attrs = []
        for key, value in ast.iter_fields(node):
            value = cls.unresolve(value, has_ast=has_ast)
            attrs.append((key, value))

        rattrs = ", ".join("%s=%s" % (key, value) for key, value in attrs)
        result = result % rattrs
        return result

    class ASTChildVisitor(ast.NodeVisitor):
        def generic_visit(self, node):
            for key, value in ast.iter_fields(node):
                if isinstance(value, AST):
                    for k, v in ast.iter_fields(value):
                        if isinstance(v, AST):
                            return True
                elif isinstance(value, list):
                    for node in value:
                        if isinstance(node, AST):
                            return True

            return False

    def __init__(self, node=None):
        self.level = 0

        if node is not None:
            self.visit(node)

    def generic_visit(self, node):
        self.level += 1
        try:
            child_visitor = self.ASTChildVisitor()
            has_ast = child_visitor.visit(node)

            print((self.level - 1) * "  ", end="")
            print(self.repr_node(node, has_ast=has_ast))

            if has_ast:
                super().generic_visit(node)
        finally:
            self.level -= 1

def print_ast_tree(node):
    printer = ASTTreePrinter()
    printer.visit(node)

def print_ast(code, mode="exec"):
    codetree = ast.parse(code, mode=mode)
    print_ast_tree(codetree)

class IsControlFlow(Exception):
    pass

class BaseBlockEnvManager(object):
    def __init__(self, **extra):
        vars(self).update(extra)
        self.reset()

    def reset(self):
        pass

class BaseDefineManager(BaseBlockEnvManager):
    def reset(self):
        self.defined = set()
        self.default_defined = NotImplemented
        self.defined_type_table = {}

    def is_defined(self, value):
        return value in self.defined

    @property
    def default_defined_type(self):
        raise NotImplementedError("default_defined is unknown.")

    def define(self, value, default_defined=None):
        if self.is_defined(value):
            raise RuntimeError("%s are already defined." % (value,))

        self.defined.add(value)

        if default_defined is not None:
            default_defined.add(value)
        else:
            if self.default_defined is NotImplemented:
                raise NotImplementedError("default_defined are not implemented.")
            self.default_defined.add(value)

        return True

    def define_short(self, name):
        if not self.is_defined(name):
            self.define(name)
            define_type = self.default_defined_type
            define_type = self.defined_type_table.get(define_type)

            if define_type:
                return define_type + " "

        return ""

class CommonDefineManager(BaseDefineManager):
    def reset(self):
        super().reset()

        self.global_defined = set()
        self.local_defined = set()
        self.defined_type_table.update({
            "local" : "local",
            "global" : "",
        })

    @property
    def default_defined_type(self):
        if self.global_defined is self.default_defined:
            return "global"
        elif self.local_defined is self.default_defined:
            return "local"
        else:
            return super().default_defined_type

    def global_define(self, value):
        return self.define(value, self.global_defined)

    def local_define(self, value):
        return self.define(value, self.local_defined)

class LuaDefineManager(CommonDefineManager):
    def reset(self):
        super().reset()

        self.default_defined = self.global_defined

class PythonDefineManager(CommonDefineManager):
    def reset(self):
        super().reset()

        self.nonlocal_defined = set()
        self.default_defined = self.local_defined
        self.defined_type_table.update({
            "nonlocal" : "",
        })

    @property
    def default_defined_type(self):
        if self.nonlocal_defined is self.default_defined:
            return "nonlocal"
        else:
            return super().default_defined_type

    def nonlocal_define(self, value):
        return self.define(value, self.nonlocal_defined)

class LuaBlockEnvManger(PythonDefineManager):
    pass

class PythonBlockEnvManger(PythonDefineManager):
    pass

class BlockBasedNodeVisitor(ast.NodeVisitor):
    # TODO: detect missed value detect! by AST Parser or Lua's Metatable!

    def __init__(self):
        self.reset()

    def reset(self):
        self.indent = 0
        self.blocks = [self.new_blockenv(first=True)]

    def new_blockenv(self, **extra):
        raise NotImplementedError

    @property
    def current_block(self):
        return self.blocks[-1]

    @contextlib.contextmanager
    def block(self, **extra):
        block_env = self.new_blockenv(**extra)
        self.enter_block(block_env)
        self.blocks.append(block_env)

        try:
            yield
        finally:
            self.blocks.pop()
            self.exit_block(block_env)

    def enter_block(self, block_env):
        pass

    def exit_block(self, block_env):
        pass

    @contextlib.contextmanager
    def noblock(self):
        try:
            yield
        except IsControlFlow:
            raise ValueError("Control Flow are not excepted.")

    @contextlib.contextmanager
    def hasblock(self):
        try:
            yield
        except IsControlFlow:
            pass

    def not_support_error(self, obj, action="AST"):
        raise TypeError("%s %r are not supported by %s" % (action, obj, type(self).__name__))

class BlockBasedCodeGenerator(BlockBasedNodeVisitor):
    def reset(self):
        super().reset()
        self.fp = io.StringIO()
        self.lastend = "\n"
        self.lineno = 1

    def enter_block(self, block_env):
        self.indent += 1
        super().enter_block(block_env)

    def exit_block(self, block_env):
        self.indent -= 1
        super().exit_block(block_env)

    def print(self, *args, **kwargs):
        fp = self.fp
        if self.lastend.endswith("\n"):
            fp.write(self.indent * self.TAB)
            self.lineno += 1
        self.lastend = kwargs.get("end", "\n")
        print(*args, file=fp, **kwargs)
        #if args == ("end",): raise

    def generic_visit(self, node):
        self.not_support_error(node, action="work with")

class FullPythonCodeTransformer(ast.NodeTransformer, BlockBasedNodeVisitor):
    def new_blockenv(self, *, scope=False, first=False, **extra):
        if not scope and not first:
            return self.current_block
        else:
            extra.update(vtemp=0)
            return PythonBlockEnvManger(**extra)

    def visit(self, node):
        if not getattr(node, "translated", False):
            return super().visit(node)
        return node

    def rvisit(self, node):
        return self.generic_visit(node)

    def rfix(self, node):
        node.translated = True
        return node

    def get_tvar(self):
        self.current_block.vtemp += 1
        vname = "_TEMP__%i__" % self.current_block.vtemp
        self.current_block.local_define(vname)

        return vname

    def make_call(self, fname, *args):
        return self.rfix(Call(
            func=Name(fname, Load()),
            args=list(map(self.rvisit, args)),
            keywords=[],
            starargs=None,
            kwargs=None,
        ))

    def make_literal(self, node, *, fname=None):
        # TODO: disable some object by apply rvisit.
        fname = fname.__name__ or type(node).__name__.lower()
        return self.rfix(Call(
            func=Name(fname, Load()),
            args=[node],
            keywords=[],
            starargs=None,
            kwargs=None,
        ))

    def make_op(self, operand, *op):
        fname = "_OP__%s__" % type(operand).__name__
        return self.make_call(fname, *op)

    def make_static_op(self, operand, *op):
        fname = "_OP__%s__" % operand
        return self.make_call(fname, *op)

    def visit_Num(self, node):
        return self.make_literal(node, fname=type(node.n))

    def visit_Str(self, node):
        return self.make_literal(node, fname=type(node.s))

    def visit_List(self, node):
        node = self.rvisit(node)
        return self.make_literal(node, fname=type(node.elts))

    def visit_Tuple(self, node):
        node = self.rvisit(node)
        return self.make_literal(node, fname=type(node.elts))

    def visit_Dict(self, node):
        node = self.rvisit(node)
        return self.make_literal(node, fname=dict)

    def visit_Set(self, node):
        node = self.rvisit(node)
        return self.make_literal(node, fname=type(node.elts))

    def visit_UnaryOp(self, node):
        return self.make_op(node.op, node.operand)

    def visit_BinOp(self, node):
        return self.make_op(node.op, node.left, node.right)

    def visit_BoolOp(self, node):
        return self.make_op(node.op, *node.values)

    def visit_Call(self, node):
        with self.noblock():
            func = self.visit(node.func)
            args = ", ".join(map(self.visit, node.args))

            assert not node.keywords
            assert node.starargs is None
            assert node.kwargs is None

            return "%s(%s)" % (func, args)

    def visit_Compare(self, node):
        ret = self.visit(node.left)

        for op, value in zip(node.ops, node.comparators):
            value = self.visit(value)
            ret = self.make_op(op, ret, value)

        return ret

    def visit_Subscript(self, node):
        return self.make_op(node, node.value, node.slice)

    def visit_Index(self, node):
        return self.rvisit(node.value)

    def visit_Slice(self, node):
        fname = type(node).__name__.lower()
        return make_call(fname, node.lower, node.upper, node.step)

    def visit_ExtSlice(self, node):
        return self.rvisit(Tuple(list(map(self.rvisit, node.dims))))

    def visit_AugAssign(self, node):
        return Assign(
            targets=[self.rvisit(node.target)],
            value=self.make_op(node, self.make_op(node.op, node.target, node.value)),
        )

    def visit_Assign(self, node):
        if len(node.targets) > 1:
            vtemp = Name(self.get_tvar(), Load())

            result = [vtemp]
            result.append(self.visit_Assign(Assign(
                targets = [vtemp],
                value = node.value,
            )))

            for target in node.targets:
                result.append(Assign(
                    targets = [target],
                    value = vtemp,
                ))

            return result

        # Starred
        def nest(target, value):
            if isinstance(target, Tuple): # or List
                vtemp = Name(self.get_tvar(), Store())
                vcount = Name(self.get_tvar(), Store())

                ret = []
                # value = iter(value)
                ret.append(Assign([vtemp], value))

                idx_starred = None
                for idx, subtarget in enumerate(target.elts):
                    if isinstance(subtarget, Starred):
                        idx_starred = idx

                def nest2(idx, subtarget):
                    if isinstance(subtarget, Starred):
                        vsubtemp = Name(self.get_tvar(), Store())
                        for i in range(len(target.elts) - idx):
                            nest2(idx, subtarget)

                        subvalue = vsubtemp
                    else:
                        subvalue = self.make_static_op("Next", vtemp)

##                    ret.append(AugAssign(
##                        vcount,
##                    ))
                    ret.append(nest(subtarget, subvalue))

                for idx, subtarget in enumerate(target.elts):
                    nest2(idx, subtarget)

                #make_static_op()

                fname = "_ERR__%s%s__" % ( # _OP__AssignTuple__
                    type(node).__name__,
                    type(target).__name__,
                )

                vname = Name("?", Load())
                ret.append(self.make_call(fname, vname, Num(n=len(target.elts))))
                return ret
            elif isinstance(target, Name) or isinstance(target, Attribute):
                return Assign([target], value)
            elif isinstance(target, Subscript):
                return self.make_op(target, target.value, target.slice, value)

        return nest(node.targets[0], node.value)

    def visit_Delete(self, node):
        return self.visit(Assign(node.targets, Name("lua.nil", Load()))) # FIXME

    def get_const(self, obj):
        assert obj is None or obj is True or obj is False # or obj is Ellipsis
        return Name("_CONST__%s__" % repr(obj), Load())

    def visit_Import(self, node):
        ret = []

        for alias in node.names:
            target = alias.asname or alias.name
            value = Str(alias.name)

            ret.append(self.visit(Assign(
                [Name(target, Store())],
                self.make_call("__import__", value),
            )))
            # TODO: from some import *, or as
            # ONLY import some and import some as other are supported.

        return ret

    def visit_If(self, node):
        node_test = self.visit(Compare(
            left=self.make_static_op(
                "Bool",
                node.test,
            ),
            ops=[Eq()],
            comparators=[self.get_const(True)],
        ))

        del node.test
        self.rvisit(node)
        node.test = node_test

        return node

    def visit_If(self, node):
        node.test = self.make_static_op("If", node.test)
        self.rvisit(node)

        return node

    def visit_IfExp(self, node):
        return self.visit_If(node)

    def visit_For(self, node):
        vtemp = Name(self.get_tvar(), Load())
        node_target = node.target
        node.target = vtemp
        node.iter = self.make_static_op(
            "Iter_For",
            node.iter,
        )

        node.body.insert(0, Assign([node_target], vtemp))

        body = []
        for line in node.body:
            line = self.visit(line)
            print(repr(line))
            if isinstance(line, list):
                body.extend(line)
            else:
                body.append(line)
        node.body = body
        return node

    def visit_For(self, node):
        node.iter = self.make_op(
            node,
            node.iter,
        )

        return node

class LiteLuaGenerator(BlockBasedCodeGenerator):
    TAB_SIZE = 2
    TAB = TAB_SIZE * " "

    Lua_True = "true"
    Lua_False = "false"
    Lua_None = "nil"

    Lua_Const = {
        Lua_True,
        Lua_False,
        Lua_None,
    }

    def new_blockenv(self, *, scope=False, first=False, **extra):
        if not scope and not first:
            return self.current_block
        else:
            return PythonBlockEnvManger(**extra)

    def generic_visit(self, node):
        self.not_support_error(node, action="work with")

    def unroll(self, body, mode=None):
        for subnode in body:
            self.print_node(subnode)
            # unroll the print_node(subnode)

    def print_node(self, subnode):
        print = self.print
        with self.hasblock():
            self.print(self.visit(subnode), end=";")
            print(" -- [LINE %i]" % subnode.lineno)

    def visit_Module(self, node):
        self.reset()
        self.unroll(node.body)

        return self.fp.getvalue()

    def visit_Interactive(self, node):
        self.reset()
        self.unroll(node.body, mode=self.hasblock)

        return self.fp.getvalue()

    def visit_Expression(self, node):
        self.reset()
        print = self.print

        # FIXME? unroll are need for single body?
        with self.noblock():
            print(self.visit(node.body))

        return self.fp.getvalue().rstrip()

    # --- Literals --- #
    def visit_Num(self, node):
        return repr(node.n)

    def visit_Str(self, node):
        return repr(node.s)

    def visit_List(self, node):
        with self.noblock():
            return "{%s}" % (", ".join(map(self.visit, node.elts)))

    def visit_Tuple(self, node):
        with self.noblock():
            return "{%s}" % (", ".join(map(self.visit, node.elts)))

    def visit_Set(self, node):
        with self.noblock():
            return "{%s}" % (", ".join(map(self.visit, node.elts)))

    def visit_Dict(self, node):
        with self.noblock():
            has_content = False
            result = "{"

            for key, value in zip(node.keys, node.values):
                has_content = True
                result += "[%s] = %s, " % (self.visit(key), self.visit(value))

            if has_content:
                result = result[:-len(", ")]

            result += "}"
            return result

    # -- Variables -- #
    def visit_Name(self, node):
        with self.noblock():
            return node.id

    # -- Expressions -- #
    def visit_Expr(self, node):
        with self.noblock():
            return self.visit(node.value)

    def visit_UnaryOp(self, node):
        with self.noblock():
            value = self.visit(node.operand)
            op = {UAdd:"", USub:"-", Not:"not", Invert:"~"}[type(node.op)]

            if not op:
                return value
            elif " " in op:
                return "%s%s" % (op, value)
            else:
                return "(%s%s)" % (op, value)

    def visit_BinOp(self, node):
        with self.noblock():
            left = self.visit(node.left)
            right = self.visit(node.right)
            op = {
                Add : "+",
                Sub : "-",
                Mult : "*",
                Div : "/",
                Mod : "%",
                Pow : "^",
            }.get(type(node.op))

            if not op:
                self.not_support_error(node.op, "op")

            return "(%s %s %s)" % (left, op, right)

    def visit_BoolOp(self, node):
        with self.noblock():
            values = list(map(self.visit, node.values))
            op = {
                And : "and",
                Or : "or",
            }[type(node.op)]

            ops = " %s " % op
            return ops.join(values)

    def visit_Compare(self, node):
        check_const = lambda x: isinstance(x, Name) and x.id in self.Lua_Const
        with self.noblock():
            assert len(node.ops) == 1
            assert len(node.comparators) == 1

            left = node.left
            op = node.ops[0]
            right = node.comparators[0]

            if isinstance(op, (Is, IsNot)):
                if not (check_const(left) or check_const(right)):
                    self.not_support_error((left, right), "Compare %r with" % op)

                op = {Is : Eq, IsNot : NotEq}[type(op)]()
            elif isinstance(op, (In, NotIn)):
                left = Subscript(
                    value = right,
                    slice = Index(left),
                    ctx = Load(),
                )
                right = Name(self.Lua_None, Load())
                op = {In : NotEq, NotIn : Eq}[type(op)]()

            left, right = map(self.visit, (left, right))
            ops = {
                Eq : "==",
                NotEq : "~=",
                Lt : "<",
                LtE : "<=",
                Gt : ">",
                GtE : ">=",
            }.get(type(op))

            if not ops:
                self.not_support_error(op, "op")

            return "%s %s %s" % (left, ops, right)

    def visit_Call(self, node):
        with self.noblock():
            func = self.visit(node.func)
            args = list(map(self.visit, node.args))

            if func == "LUA_CODE":
                assert len(node.args) == 1
                assert isinstance(node.args[0], Str)
                return node.args[0].s

            assert not node.keywords
            assert node.kwargs is None

            if node.starargs:
                block = self.current_block
                vararg = getattr(block, "vararg", None)
                if isinstance(node.starargs, Name) and node.starargs.id == vararg:
                    args.append("...")
                else:
                    args.append("unpack(%s)" % (self.visit(node.starargs)))

            args = ", ".join(args)
            return "%s(%s)" % (func, args)

    def visit_arg(self, node):
        # TODO: remove it and put to visit_Call
        with self.noblock():
            return node.arg

    def visit_IfExp(self, node):
        with self.noblock():
            test = self.visit(node.test)
            body = self.visit(node.body)
            orelse = self.visit(node.orelse)

            return "((%s and {%s} or {%s})[1])" % (test, body, orelse)

    def visit_Attribute(self, node):
        with self.noblock():
            value = self.visit(node.value)
            attr = node.attr

            return "%s.%s" % (value, attr)

    # -- Subscripting -- #
    # visit_Subscript must be not direct call from visit, it must different
    #  when assign or see the value??
    def visit_Subscript(self, node):
        with self.noblock():
            name = self.visit(node.value)
            index = self.visit(node.slice)

            return "%s[%s]" % (name, index)

    def visit_Index(self, node):
        with self.noblock():
            return self.visit(node.value)

    # -- Comprehensions -- #

    # -- Statements -- #
    def _get_Name(self, node):
        def nest(x, pure=True):
            if isinstance(x, (Attribute, Subscript)):
                return nest(x.value, pure=False)
            elif isinstance(x, Name):
                return x.id, pure
            return None, False
        return nest(node)

    def visit_AugAssign(self, node):
        with self.noblock():
            AssignAST = ast.Assign(
                targets=[node.target],
                value = ast.BinOp(node.target, node.op, node.value),
            )

            name, _ = self._get_Name(node.target)
            block = self.current_block

            if not block.is_defined(name):
                raise RuntimeError("Name %s are not defined." % name)

        with self.hasblock():
            self.visit(AssignAST)

        raise IsControlFlow

    def visit_Assign(self, node):
        with self.noblock():
            assert len(node.targets) == 1, node.targets
            rawtarget = node.targets[0]
            if isinstance(rawtarget, Tuple):
                target = ", ".join(map(self.visit, rawtarget.elts))
            else:
                target = self.visit(rawtarget)
            value = self.visit(node.value)

        define_type = ""
        name, pure = self._get_Name(rawtarget)
        if name is not None and pure:
            block = self.current_block
            define_type = block.define_short(name)

        self.print("%s%s = %s" % (define_type, target, value), end=";\n")

        raise IsControlFlow

    def visit_Assert(self, node):
        with self.noblock():
            args = [node.test]

            if node.msg is not None:
                args.append(node.msg)

            return self.visit(Call(
                func = Name(type(node).__name__.lower(), Load()),
                args = args,
                keywords = [],
                starargs=None,
                kwargs=None,
            ))

    def visit_Delete(self, node):
        for target in node.targets:
            with self.hasblock():
                assignAST = Assign([target], Name(self.Lua_None, Load()))
                self.visit_Assign(assignAST)

    def visit_Pass(self, node):
        raise IsControlFlow

    # -- Imports -- #
    # Imports are not accepted in cc's lua. use os.loadAPI

    # -- Control flow -- #
    def visit_If(self, node):
        print = self.print

        print("if", self.visit(node.test), "then")
        with self.block():
            self.unroll(node.body)

        if node.orelse:
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                print("else", end="")
                with self.hasblock():
                    self.visit_If(node.orelse[0])
                raise IsControlFlow
            else:
                print("else")
                with self.block():
                    self.unroll(node.orelse)
        print("end", end=";\n")

        raise IsControlFlow

    def _visit_Loop(self, node):
        print = self.print
        zrand = random.randint(100000, 999999)
        hascont = False
        contname = ""
        hasbreak = False
        breakname = ""
        # TODO: break, continue in block define?

        with self.noblock():
            with self.block():
                for subnode in node.body:
                    if isinstance(subnode, ast.Continue):
                        hascont = True
                        contname = "ZCONT_%i" % zrand
                        print("goto ", contname, '; -- continue', sep="")
                    elif isinstance(subnode, ast.Break) and node.orelse:
                        hasbreak = True
                        breakname = "ZBREAK_%i" % zrand
                        print("goto ", breakname, '; -- break', sep="")
                    else:
                        self.print_node(subnode)

                if hascont:
                    print("::", contname, "::", sep="")
            print("end", end=";\n")

            if node.orelse:
                self.unroll(node.orelse)

            if hasbreak:
                print("::", breakname, "::", sep="")

        raise IsControlFlow

    def visit_For(self, node):
        print = self.print

        with self.noblock():
            if isinstance(node.target, Tuple):
                block = self.current_block
                targets = []
                for subnode in node.target.elts:
                    name, _ = self._get_Name(subnode)
                    if block.define_short(name):
                        targets.append(name)

                if targets:
                    # FIXME
                    print(block.default_defined_type, ", ".join(targets), end=";\n")

                target = ", ".join(map(self.visit, node.target.elts))
            elif isinstance(node.target, Name):
                target = self.visit(node.target)
            else:
                self.not_support_error(node.target)

            iter = self.visit(node.iter)

        print("for", target, "in", iter, "do")
        self._visit_Loop(node)

    def visit_While(self, node):
        print = self.print

        with self.noblock():
            test = self.visit(node.test)

        print("while", test, "do")
        self._visit_Loop(node)

    # -- Function and class definitions -- #
    def _visit_Decorators(self, node):
        print = self.print

        for decorator in node.decorator_list:
            with self.noblock():
                print("%s = %s(%s)" % (fname, self.visit(decorator), fname), end=";\n")

        raise IsControlFlow

    def visit_FunctionDef(self, node):
        print = self.print
        fname = node.name
        fargs = list(map(self.visit, node.args.args))

        vararg = node.args.vararg
        if vararg:
            fargs.append("...")

        #assert not node.args.vararg
        assert not node.args.kwonlyargs
        assert not node.args.varargannotation
        assert not node.args.kwarg
        assert not node.args.kwargannotation
        assert not node.args.defaults
        assert not node.args.kw_defaults
        assert not node.returns

        block = self.current_block
        define_type = block.define_short(fname)

        fargs = ", ".join(fargs)
        print("%sfunction %s(%s)" % (define_type, fname, fargs))
        with self.block(scope=True):
            block = self.current_block

            if vararg:
                block.local_define(vararg)
                print("local %s = {...};" % (vararg,))
                block.vararg = vararg

            self.unroll(node.body)
        print("end", end=";\n")

        with self.hasblock():
            self._visit_Decorators(node)

        raise IsControlFlow

    def visit_Lambda(self, node):
        with self.noblock():
            args = list(map(self.visit, node.args.args))

        vararg = node.args.vararg
        if vararg:
            args.append("...")

        assert not node.args.kwonlyargs
        assert not node.args.varargannotation
        assert not node.args.kwarg
        assert not node.args.kwargannotation
        assert not node.args.defaults
        assert not node.args.kw_defaults

        # TODO: don't use unpack in here? (*args)

        class LambdaVarargTransfomer(ast.NodeTransformer):
            def visit_Name(self, node):
                if node.id == vararg:
                    return List([Name("...", Load())], Store()) # FIXME
                return node

        if vararg:
            transformer = LambdaVarargTransfomer()
            node.body = transformer.visit(node.body)

        args = ", ".join(args)
        result = "(function(%s) return " % args
        with self.noblock():
            result += self.visit(node.body)
        result += " end)"

        return result

    def visit_Return(self, node):
        return "return %s" % self.visit(node.value)

    def visit_Global(self, node):
        block = self.current_block
        for name in node.names:
            block.global_define(name)

        # self.print("-- global %s" % ", ".join(node.names))
        raise IsControlFlow

    def visit_Nonlocal(self, node):
        block = self.current_block
        for name in node.names:
            block.nonlocal_define(name)

        # self.print("-- nonlocal %s" % ", ".join(node.names))
        raise IsControlFlow

    def visit_ClassDef(self, node):
        print = self.print

        assert not node.starargs
        assert not node.kwargs

        metatable = None
        for keyword in node.keywords:
            if keyword.arg != "metatable":
                raise NotImplementedError("PEP-3115 are not supported in %s" % type(self).__name__)

            metatable = self.visit(keyword.value)

        name = node.name
        bases = node.bases
        block = self.current_block
        define_type = block.define_short(name)

        bases = []
        for base in node.bases:
            bases.append(self.visit(base))

        clsfmt = define_type, name, name, bases and ":" or "", ", ".join(bases)
        print("%s%s = (function(_G) -- (class %s%s%s)" % clsfmt)
        with self.block(scope=True):
            print("setfenv(1, setmetatable({}, {_G=_G, __index=_G}));")
            if bases:
                print("(function(o,c,k,v)")
                print("  for k,c in pairs({%s}) do" % ", ".join(bases[::-1]))
                print("    for k,v in pairs(c) do o[k]=v end")
                print("  end")
                print("end)(getfenv());")
            print("__name__", "=", repr(name), end=";\n")

            block = self.current_block
            block.default_defined = block.global_defined
            # TODO: change type_table for assign out of this class.

            self.unroll(node.body)

            print("return getfenv()", end=";\n")
        print("end)(getfenv())", end=";\n")

        if metatable:
            print("setmetatable(%s, %s)" % (name, metatable), end=";\n")

        with self.hasblock():
            self._visit_Decorators(node)

        raise IsControlFlow

_DEFAULT_COMPILE_MODE = "exec"

def lua_compile(code, codetype, mode=_DEFAULT_COMPILE_MODE):
    codetree = ast.parse(code, mode=mode)

    if codetype == CTYPE_LITE:
        pass
    elif codetype == CTYPE_FULL:
        cls = FullPythonCodeTransformer()
        cls.visit(codetree)
    else:
        raise ValueError("codetype must in %r" % {CTYPE_LITE, CTYPE_FULL})

    return LiteLuaGenerator().visit(codetree)

def lua_lite_compile(code, mode=_DEFAULT_COMPILE_MODE):
    return lua_compile(code, CTYPE_LITE, mode=mode)

def lua_full_compile(code, mode=_DEFAULT_COMPILE_MODE):
    return lua_compile(code, CTYPE_FULL, mode=mode)

def execute_lite(filename, fromfile=None):
    # TODO: Clean me!?
    if os.sep in os.path.normpath(filename):
        # FIXME LATER!
        raise RuntimeError("Can't guess lua pattern of error")

    if fromfile:
        fromfile = os.path.abspath(fromfile)
    else:
        fromfile = filename

    filetable = {}

    def get_lineno(filename, lineno):
        if filename not in filetable:
            filenotable = {}
            filetable[filename] = filenotable
            lastline = None

            with open(filename, 'r') as fp:
                for no, line in enumerate(fp, 1):
                    line = line.rstrip('\r\n')
                    if "-- [LINE " in line:
                        a, b, c = line.rpartition("-- [LINE ")
                        d, e, f = c.partition("]")
                        assert a and b and e and not f
                        lastline = int(d)

                    filenotable[no] = lastline

        return filetable[filename][lineno]

    def parse_tb(line):
        trace, sep, detail = line.partition(": ")
        assert(sep)
        if ":" in trace:
            tracename, sep, lineno = trace.partition(":")
            lineno = int(lineno)

            if tracename == filename:
                realno = get_lineno(tracename, lineno)
                if realno:
                    fmt = "  File \"%s\", line %i, %s"
                    print(fmt % (fromfile, realno, detail))
                    return

            fmt = "  File %r, line %i, %s"
            print(fmt % (trace, lineno, detail))
            return
        else:
            fmt = "  File %r, %s"
            print(fmt % (trace, detail))
            return

    import subprocess
    import types

    from subprocess import PIPE
    process = subprocess.Popen(["tools/lua/lua5.1.exe", filename], stdout=PIPE, stderr=PIPE)
    stdout, stderr = map(bytes.decode, process.communicate())

    stdout = stdout.strip()
    if stdout:
        print(stdout)

    tbline = None
    lastline = None
    stderr = stderr.strip()
    if stderr:
        for line in stderr.splitlines():
            if lastline is None:
                lastline = line
                continue
            if line == "stack traceback:":
                tbline = lastline
                print()
                print("Traceback (most recent call last):")
                continue
            elif line.startswith("\t"):
                parse_tb(line.lstrip("\t"))
                continue

            lastline = line
            print(line)

        if tbline:
            print("Exception: " + tbline.rpartition(": ")[2])

def compile_and_run_py_API():
    filename_api = "py_API"
    filename_api_py = filename_api + ".py"
    filename_api_lua = filename_api + ".lua"

    with open(filename_api_py) as fp:
        code = fp.read()

    compiled = lua_lite_compile(code)
    print("Success compiled with size:", len(compiled))

    with open(filename_api_lua, "w") as fp:
        fp.write(compiled)

    if os.getlogin() == "EcmaXp":
        # Only for me :D
        with open(r"X:\Data\Workspace\newlua\src\py_API.lua", "w") as fp:
            fp.write(compiled)

    execute_lite(filename_api_lua, filename_api_py)

def main():
    compile_and_run_py_API()

    print(lua_lite_compile("""\
class t:
    def _test(self):
        print(self.__tes)
"""), end="")

if __name__ == '__main__':
    main()