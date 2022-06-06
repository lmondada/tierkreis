from typing import Iterable, Optional, Tuple, Union, overload

import betterproto

# Awkwardly, the Rust stubs end up here:
from tierkreis import tierkreis as tierkreis_type_inference
from tierkreis import TierkreisGraph
from tierkreis.core.function import TierkreisFunction
from tierkreis.core.types import TierkreisTypeErrors
from tierkreis.core.values import StructValue
import tierkreis.core.protos.tierkreis.signature as ps
import tierkreis.core.protos.tierkreis.graph as pg
from . import RuntimeSignature


@overload
def infer_graph_types(
    g: TierkreisGraph,
    funcs: Union[Iterable[TierkreisFunction], RuntimeSignature],
    inputs: None = None,
) -> TierkreisGraph:
    ...


@overload
def infer_graph_types(
    g: TierkreisGraph,
    funcs: Union[Iterable[TierkreisFunction], RuntimeSignature],
    inputs: StructValue,
) -> Tuple[TierkreisGraph, StructValue]:
    ...


def infer_graph_types(
    g: TierkreisGraph,
    funcs: Union[Iterable[TierkreisFunction], RuntimeSignature],
    inputs: Optional[StructValue] = None,
) -> Union[TierkreisGraph, Tuple[TierkreisGraph, StructValue]]:
    func_list = (
        [func for nsdefs in funcs.values() for func in nsdefs.functions.values()]
        if isinstance(funcs, dict)
        else funcs
    )

    req = ps.InferGraphTypesRequest(
        gwi=ps.GraphWithInputs(
            graph=g.to_proto(),
            inputs=None
            if inputs is None
            else pg.StructValue(map=inputs.to_proto_dict()),
        ),
        functions={func.name: func.to_proto() for func in func_list},
    )
    resp = ps.InferGraphTypesResponse().parse(
        tierkreis_type_inference.infer_graph_types(bytes(req))
    )
    name, _ = betterproto.which_one_of(resp, "response")
    if name == "success":
        g = TierkreisGraph.from_proto(resp.success.graph)
        if inputs is None:
            assert resp.success.inputs is None
            return g
        return (g, StructValue.from_proto(resp.success.inputs))
    raise TierkreisTypeErrors.from_proto(resp.error)