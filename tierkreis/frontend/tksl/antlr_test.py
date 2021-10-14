from dataclasses import dataclass, field, make_dataclass
from pathlib import Path
import copy
import asyncio
from collections import OrderedDict
from typing import Any, Iterable, Dict, List, Optional, Tuple
from tierkreis import TierkreisGraph
from tierkreis.core.function import TierkreisFunction
from tierkreis.core.tierkreis_graph import NodePort, NodeRef, TierkreisEdge
from tierkreis.core.types import (
    BoolType,
    CircuitType,
    FloatType,
    GraphType,
    IntType,
    MapType,
    PairType,
    Row,
    StringType,
    StructType,
    TierkreisType,
    VecType,
    VarType,
)
from tierkreis.core.tierkreis_struct import TierkreisStruct
from tierkreis.frontend import local_runtime
from tierkreis.core.graphviz import tierkreis_to_graphviz


from antlr4 import InputStream, CommonTokenStream
from tierkreis.frontend.tksl.antlr.TkslParser import TkslParser  # type: ignore
from tierkreis.frontend.tksl.antlr.TkslLexer import TkslLexer  # type: ignore
from tierkreis.frontend.tksl.antlr.TkslVisitor import TkslVisitor  # type: ignore


@dataclass
class FunctionDefinition:
    inputs: list[str]
    outputs: list[str]
    graph_type: Optional[GraphType] = None


FuncDefs = Dict[str, Tuple[TierkreisGraph, FunctionDefinition]]
PortMap = OrderedDict[str, Optional[TierkreisType]]
Aliases = Dict[str, TierkreisType]
NamespaceDict = Dict[str, TierkreisFunction]
RuntimeSignature = Dict[str, NamespaceDict]


@dataclass
class Context:
    functions: FuncDefs = field(default_factory=dict)
    output_vars: Dict[str, Tuple[NodeRef, FunctionDefinition]] = field(
        default_factory=dict
    )
    constants: Dict[str, NodeRef] = field(default_factory=dict)

    inputs: PortMap = field(default_factory=OrderedDict)
    outputs: PortMap = field(default_factory=OrderedDict)

    aliases: Aliases = field(default_factory=dict)

    def copy(self) -> "Context":
        return copy.deepcopy(self)


def def_from_tkfunc(func: TierkreisFunction) -> FunctionDefinition:
    return FunctionDefinition(
        func.input_order,
        func.output_order,
    )


def make_outports(node_ref: NodeRef, ports: Iterable[str]) -> List[NodePort]:
    return [node_ref[outport] for outport in ports]


class TkslTopVisitor(TkslVisitor):
    def __init__(self, signature: RuntimeSignature, context: Context):
        self.sig = signature
        self.context = context.copy()
        self.graph = TierkreisGraph()

    def visitBool_token(self, ctx: TkslParser.Bool_tokenContext) -> bool:
        if ctx.TRUE():
            return True
        return False

    def visitInport(self, ctx: TkslParser.InportContext) -> str:
        return str(ctx.ID())

    def visitPort_label(self, ctx: TkslParser.Port_labelContext) -> NodePort:
        var_name = str(ctx.ID(0))
        port_name = str(ctx.ID(1))
        noderef, _ = self.context.output_vars[var_name]
        return NodePort(noderef, port_name)

    def visitF_name(
        self, ctx: TkslParser.F_nameContext
    ) -> Tuple[str, FunctionDefinition]:
        func_name = ctx.func_name.text
        namespace = ctx.namespace.text if ctx.ID(1) else "builtin"
        try:
            tkfunc = self.sig[namespace][func_name]
            func_name = tkfunc.name
            return func_name, def_from_tkfunc(tkfunc)
        except KeyError as err:
            if func_name in self.context.functions:
                return func_name, self.context.functions[func_name][1]
                primitive = False
            else:
                raise RuntimeError(f"Function name not found: {func_name}") from err

    def visitThunkable_port(
        self, ctx: TkslParser.Thunkable_portContext
    ) -> List[NodePort]:
        if ctx.port_label():
            return [self.visitPort_label(ctx.port_label())]
        if ctx.ID():
            name = str(ctx.ID())
            if name in self.context.inputs:
                return [self.graph.input[name]]
            if name in self.context.output_vars:
                node_ref, func = self.context.output_vars[name]
                return make_outports(node_ref, func.outputs)
            if name in self.context.functions:
                grap, _ = self.context.functions[name]
                const_node = self.graph.add_const(grap)
                return [const_node["value"]]
            if name in self.context.constants:
                return [self.context.constants[name]["value"]]
            raise RuntimeError(f"Name not found in scope: {name}.")
        raise RuntimeError()

    def visitOutport(self, ctx: TkslParser.OutportContext) -> List[NodePort]:
        if ctx.thunkable_port():
            return self.visitThunkable_port(ctx.thunkable_port())
        if ctx.node_inputs():
            node_ref, fun = self.visit(
                ctx.node_inputs()
            )  # relies on correct output here
            return make_outports(node_ref, fun.outputs)
        if ctx.const_():
            node_ref = self.graph.add_const(self.visitConst_(ctx.const_()))
            return [node_ref["value"]]
        raise RuntimeError()

    def visitPort_map(self, ctx: TkslParser.Port_mapContext) -> Tuple[str, NodePort]:
        # only one outport in portmap
        return self.visitInport(ctx.inport()), self.visitOutport(ctx.outport())[0]

    def visitPositional_args(
        self, ctx: TkslParser.Positional_argsContext
    ) -> List[NodePort]:
        return sum(map(self.visitOutport, ctx.arg_l), [])

    def visitNamed_map(self, ctx: TkslParser.Named_mapContext) -> Dict[str, NodePort]:
        return dict(map(self.visitPort_map, ctx.port_l))

    def visitArglist(self, ctx: TkslParser.ArglistContext) -> Dict[str, NodePort]:
        if ctx.named_map():
            return self.visitNamed_map(ctx.named_map())
        if ctx.positional_args():
            assert hasattr(ctx, "expected_ports")
            return dict(
                zip(
                    ctx.expected_ports, self.visitPositional_args(ctx.positional_args())
                )
            )
        raise RuntimeError()

    def visitFuncCall(
        self, ctx: TkslParser.FuncCallContext
    ) -> Tuple[NodeRef, FunctionDefinition]:
        f_name, f_def = self.visitF_name(ctx.f_name())
        arglist = {}
        if hasattr(ctx, "arglist"):
            argctx = ctx.arglist()
            argctx.expected_ports = f_def.inputs
            arglist = self.visitArglist(argctx)

        if f_name in self.context.functions:
            noderef = self.graph.add_box(
                self.context.functions[f_name][0], f_name, **arglist
            )
        else:
            noderef = self.graph.add_node(f_name, **arglist)

        return noderef, f_def

    def visitThunk(
        self, ctx: TkslParser.ThunkContext
    ) -> Tuple[NodeRef, FunctionDefinition]:
        outport = self.visitThunkable_port(ctx.thunkable_port())[0]
        arglist = self.visitNamed_map(ctx.named_map()) if ctx.named_map() else {}
        eval_n = self.graph.add_node("builtin/eval", thunk=outport, **arglist)
        return eval_n, def_from_tkfunc(self.sig["builtin"]["eval"])

    def visitCallMap(self, ctx: TkslParser.CallMapContext) -> None:
        target = ctx.target.text
        self.context.output_vars[target] = self.visit(ctx.call)

    def visitOutputCall(self, ctx: TkslParser.OutputCallContext) -> None:
        argctx = ctx.arglist()
        argctx.expected_ports = list(self.context.outputs)
        self.graph.set_outputs(**self.visitArglist(argctx))

    def visitConstDecl(self, ctx: TkslParser.ConstDeclContext) -> None:
        target = ctx.const_name.text
        const_val = self.visitConst_(ctx.const_())
        self.context.constants[target] = self.graph.add_const(const_val)

    def visitIfBlock(self, ctx: TkslParser.IfBlockContext):
        target = ctx.target.text
        condition = self.visitOutport(ctx.condition)[0]
        inputs = self.visitNamed_map(ctx.inputs) if ctx.inputs else {}

        ifcontext = Context()
        ifcontext.functions = self.context.functions.copy()
        ifcontext.inputs = OrderedDict({inp: None for inp in inputs})
        # outputs from if-else block have to be named map (not positional)

        ifvisit = TkslTopVisitor(self.sig, ifcontext)
        if_g = ifvisit.visitCode_block(ctx.if_block)

        elsevisit = TkslTopVisitor(self.sig, ifcontext)
        else_g = elsevisit.visitCode_block(ctx.else_block)

        sw_nod = self.graph.add_node(
            "builtin/switch", pred=condition, if_true=if_g, if_false=else_g
        )
        eval_n = self.graph.add_node("builtin/eval", thunk=sw_nod["value"], **inputs)

        output_names = set(if_g.outputs()).union(else_g.outputs())
        ifcontext.outputs = OrderedDict({outp: None for outp in output_names})

        fake_func = FunctionDefinition(list(ifcontext.inputs), list(ifcontext.outputs))
        self.context.output_vars[target] = (eval_n, fake_func)

    def visitLoop(self, ctx: TkslParser.LoopContext):
        target = ctx.target.text
        inputs = self.visitNamed_map(ctx.inputs) if ctx.inputs else {}

        loopcontext = Context()
        loopcontext.functions = self.context.functions.copy()
        loopcontext.inputs = OrderedDict({inp: None for inp in inputs})
        # outputs from if-else block have to be named map (not positional)

        bodyvisit = TkslTopVisitor(self.sig, loopcontext)
        body_g = bodyvisit.visitCode_block(ctx.body)

        conditionvisit = TkslTopVisitor(self.sig, loopcontext)
        condition_g = conditionvisit.visitCode_block(ctx.condition)

        loop_nod = self.graph.add_node(
            "builtin/loop", condition=condition_g, body=body_g, **inputs
        )

        loopcontext.outputs = OrderedDict({outp: None for outp in body_g.outputs()})

        fake_func = FunctionDefinition(
            list(loopcontext.inputs), list(loopcontext.outputs)
        )
        self.context.output_vars[target] = (loop_nod, fake_func)

    def visitEdge(self, ctx: TkslParser.EdgeContext) -> TierkreisEdge:
        return self.graph.add_edge(
            self.visitPort_label(ctx.source), self.visitPort_label(ctx.target)
        )

    def visitCode_block(self, ctx: TkslParser.Code_blockContext) -> TierkreisGraph:
        _ = list(map(self.visit, ctx.inst_list))
        return self.graph

    def visitFuncDef(self, ctx: TkslParser.FuncDefContext):
        name = str(ctx.ID())
        f_def = self.visitGraph_type(ctx.graph_type())
        context = self.context.copy()
        context.inputs = OrderedDict(
            (key, f_def.graph_type.inputs.content[key]) for key in f_def.inputs
        )
        context.outputs = OrderedDict(
            (key, f_def.graph_type.outputs.content[key]) for key in f_def.outputs
        )

        def_visit = TkslTopVisitor(self.sig, context)
        graph = def_visit.visitCode_block(ctx.code_block())

        self.context.functions[name] = (graph, f_def)

    def visitTypeAlias(self, ctx: TkslParser.TypeAliasContext):
        self.context.aliases[str(ctx.ID())] = self.visitType_(ctx.type_())

    def visitStart(self, ctx: TkslParser.StartContext) -> TierkreisGraph:
        _ = list(map(self.visit, ctx.decs))

        return self.context.functions["main"][0]

    def visitStruct_id(self, ctx: TkslParser.Struct_idContext) -> Optional[str]:
        if ctx.TYPE_STRUCT():
            return None
        if ctx.ID():
            return str(ctx.ID())
        raise RuntimeError()

    def visitConst_assign(self, ctx: TkslParser.Const_assignContext) -> Tuple[str, Any]:
        return str(ctx.ID()), self.visitConst_(ctx.const_())

    def visitConst_(self, ctx: TkslParser.Const_Context) -> Any:
        if ctx.SIGNED_INT():
            return int(str(ctx.SIGNED_INT()))
        if ctx.bool_token():
            return self.visitBool_token(ctx.bool_token())
        if ctx.SIGNED_FLOAT():
            return float(str(ctx.SIGNED_FLOAT()))
        if ctx.SHORT_STRING():
            return str(ctx.SHORT_STRING())[1:-1]
        if ctx.vec_const():
            return list(map(self.visitConst_, ctx.vec_const().elems))
        if ctx.struct_const():
            struct_ctx = ctx.struct_const()
            _struct_id = str(struct_ctx.sid)
            fields = dict(map(self.visitConst_assign, struct_ctx.fields))
            cl = make_dataclass(
                "anon_struct", fields=fields.keys(), bases=(TierkreisStruct,)
            )
            return cl(**fields)

    def visitF_param(self, ctx: TkslParser.F_paramContext) -> Tuple[str, TierkreisType]:
        return ctx.label.text, self.visitType_(ctx.annotation)

    def visitF_param_list(
        self, ctx: TkslParser.F_param_listContext
    ) -> OrderedDict[str, TierkreisType]:
        return OrderedDict(map(self.visitF_param, ctx.par_list))

    def visitGraph_type(self, ctx: TkslParser.Graph_typeContext) -> FunctionDefinition:
        inputs = self.visitF_param_list(ctx.inputs)
        outputs = self.visitF_param_list(ctx.outputs)
        g_type = GraphType(
            inputs=Row(inputs),
            outputs=Row(outputs),
        )
        return FunctionDefinition(list(inputs), list(outputs), g_type)

    def visitType_(self, ctx: TkslParser.Type_Context) -> TierkreisType:
        if ctx.TYPE_INT():
            return IntType()
        if ctx.TYPE_BOOL():
            return BoolType()
        if ctx.TYPE_STR():
            return StringType()
        if ctx.TYPE_FLOAT():
            return FloatType()
        if ctx.TYPE_PAIR():
            pair_type = PairType(self.visit(ctx.first), self.visit(ctx.second))
            return pair_type
        if ctx.TYPE_MAP():
            return MapType(self.visit(ctx.key), self.visit(ctx.val))
        if ctx.TYPE_VEC():
            return VecType(self.visit(ctx.element))
        if ctx.TYPE_STRUCT():
            return StructType(Row(self.visit(ctx.fields)))
        if ctx.graph_type():
            g_type = self.visitGraph_type(ctx.graph_type()).graph_type
            assert g_type is not None
            return g_type
        if ctx.ID():
            return self.context.aliases[str(ctx.ID())]
            # if type_name == "TYPE_MAP":
            #     return MapType(
            #         get_type(token.children[1], aliases), get_type(token.children[2], aliases)
            #     )
            # if type_name == "TYPE_VEC":
            #     return VecType(get_type(token.children[1], aliases))
            # if type_name == "TYPE_STRUCT":
            #     args = token.children[1].children
            #     return StructType(
            #         Row(
            #             {
            #                 arg.children[0].value: get_type(arg.children[1], aliases)
            #                 for arg in args
            #             }
            #         )
            #     )
            # if type_name == "TYPE_CIRCUIT":
            #     return CircuitType()
            # if token.data == "alias":
            # return aliases[token.children[0].value]
        return VarType("unkown")

    def visitDeclaration(self, ctx: TkslParser.DeclarationContext) -> None:

        if ctx.TYPE():
            self.context.aliases[ctx.alias.text] = self.visit(ctx.type_def)
        return self.visitChildren(ctx)


from grpclib.client import Channel
from tierkreis.frontend.myqos_client import MyqosClient
import ssl


async def main():
    with open("antlr_sample.tksl") as f:
        text = f.read()
    lexer = TkslLexer(InputStream(text))
    stream = CommonTokenStream(lexer)
    parser = TkslParser(stream)

    # parser.removeErrorListeners()
    # errorListener = ChatErrorListener(self.error)

    tree = parser.start()
    exe = Path("../../../../target/debug/tierkreis-server")
    # ssl._create_default_https_context = ssl._create_unverified_context
    # context = ssl._create_unverified_context()
    # context.check_hostname = True
    # context.verify_mode = ssl.CERT_OPTIONAL
    # context.verify_flags = ssl.VERIFY_CRL_CHECK_LEAF

    # async with Channel('tierkreistrr595bx-staging-pr.uksouth.cloudapp.azure.com',443, ssl=True) as channel:
    # client = MyqosClient(channel)
    async with local_runtime(exe) as client:
        sig = await client.get_signature()
        out = TkslTopVisitor(sig, Context()).visitStart(tree)
        out = await client.type_check_graph(out)
        # print(await client.run_graph(out, {"v1": 67, "v2": (45, False)}))
    #     # print(await channel.__connect__())
    #     client = MyqosClient(channel)
    #     sig = await client.get_signature()

    tierkreis_to_graphviz(out).render("dump", "png")


asyncio.run(main())
