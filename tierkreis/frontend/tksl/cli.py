import asyncio
import pprint
import traceback
from functools import wraps
import sys
from pathlib import Path
from typing import AsyncContextManager, Dict, Optional, TextIO, cast

import click
from tierkreis.frontend.runtime_client import RuntimeSignature
from yachalk import chalk
from antlr4.error.Errors import ParseCancellationException  # type: ignore
from tierkreis import TierkreisGraph
from tierkreis.core.graphviz import tierkreis_to_graphviz
from tierkreis.core.types import TierkreisType, TierkreisTypeErrors, TypeScheme
from tierkreis.frontend import RuntimeClient, DockerRuntime, local_runtime
from tierkreis.frontend.tksl import parse_tksl
from tierkreis.frontend.myqos_client import myqos_runtime

LOCAL_SERVER_PATH = Path(__file__).parent / "../../../../target/debug/tierkreis-server"
RUNTIME_LABELS = ["docker", "local", "myqos"]


def coro(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


async def _parse(source: TextIO, client: RuntimeClient) -> TierkreisGraph:
    try:
        return parse_tksl(source.read(), await client.get_signature())
    except ParseCancellationException as _parse_err:
        print(chalk.red(f"Parse error: {str(_parse_err)}"), file=sys.stderr)
        exit()


async def _check_graph(
    source_path: Path, client_manager: AsyncContextManager[RuntimeClient]
) -> TierkreisGraph:
    async with client_manager as client:
        with open(source_path, "r") as f:
            tkg = await _parse(f, client)
        try:
            tkg = await client.type_check_graph(tkg)
        except TierkreisTypeErrors as _errs:
            print(chalk.red(traceback.format_exc(0)), file=sys.stderr)
            exit()
        return tkg


@click.group()
@click.pass_context
@click.option(
    "--runtime",
    "-R",
    type=click.Choice(RUNTIME_LABELS, case_sensitive=True),
    default="local",
)
@coro
async def cli(ctx: click.Context, runtime: str):
    ctx.ensure_object(dict)
    ctx.obj["runtime_label"] = runtime
    if runtime == "myqos":
        client_manager = myqos_runtime(
            "tierkreistrr595bx-pr.uksouth.cloudapp.azure.com"
        )
    elif runtime == "docker":
        client_manager = DockerRuntime("cqc/tierkreis")
    else:
        assert LOCAL_SERVER_PATH.exists()
        client_manager = local_runtime(LOCAL_SERVER_PATH)
    asyncio.get_event_loop()
    ctx.obj["client_manager"] = client_manager


@cli.command()
@click.argument("source", type=click.Path(exists=True))
@click.option(
    "--target",
    type=click.Path(exists=False),
    help="target file to write protobuf binary to.",
)
@click.pass_context
@coro
async def build(ctx: click.Context, source: str, target: Optional[str]):
    source_path = Path(source)
    if target:
        target_path = Path(target)
    else:
        assert source_path.suffix == ".tksl"
        target_path = source_path.with_suffix(".bin")
    tkg = await _check_graph(source_path, ctx.obj["client_manager"])

    with open(target_path, "wb") as f:
        f.write(bytes(tkg.to_proto()))


@cli.command()
@click.argument("source", type=click.Path(exists=True))
@click.pass_context
@coro
async def check(ctx: click.Context, source: str) -> TierkreisGraph:
    source_path = Path(source)
    tkg = await _check_graph(source_path, ctx.obj["client_manager"])
    print(chalk.bold.green("Success: graph type check complete."))
    return tkg


@cli.command()
@click.argument("source", type=click.Path(exists=True))
@click.argument("view_path", type=click.Path(exists=False))
@click.option("--inline", is_flag=True)
@click.option("--check", "-C", is_flag=True)
@click.option("--recursive", is_flag=True)
@click.pass_context
@coro
async def view(
    ctx: click.Context,
    source: str,
    view_path: str,
    inline: bool,
    recursive: bool,
    check: bool,
):
    source_path = Path(source)
    if check:
        tkg = await _check_graph(source_path, ctx.obj["client_manager"])
    else:
        async with ctx.obj["client_manager"] as client:
            with open(source_path, "r") as f:
                tkg = await _parse(f, client)
    if inline:
        tkg = tkg.inline_boxes(recursive=recursive)
    tkg.name = source_path.stem
    view_p = Path(view_path)
    ext = view_p.suffix
    tierkreis_to_graphviz(tkg).render(view_path[: -len(ext)], format=ext[1:])


@cli.command()
@click.argument("source", type=click.File("r"))
@click.pass_context
@coro
async def run(ctx: click.Context, source: TextIO):
    async with ctx.obj["client_manager"] as client:
        tkg = await _parse(source, client)
        try:
            outputs = await client.run_graph(tkg, {})
            pprint.pprint(
                {key: val.to_proto().to_json() for key, val in outputs.items()}
            )
        except TierkreisTypeErrors as _errs:
            print(chalk.red(traceback.format_exc(0)), file=sys.stderr)


def _arg_str(args: Dict[str, TierkreisType]) -> str:
    return ", ".join(
        f"{chalk.yellow(port)}: {str(_type)}" for port, _type in args.items()
    )


def _print_namespace(sig: RuntimeSignature, namespace: str, function: Optional[str]):
    print(chalk.bold(f"Namespace: {namespace}"))
    print()
    names_dict = sig[namespace]
    func_names = [function] if function else list(names_dict.keys())
    for name in sorted(func_names):
        func = names_dict[name]
        inputs = {
            port: func.type_scheme.body.inputs.content[port]
            for port in func.input_order
        }
        outputs = {
            port: func.type_scheme.body.outputs.content[port]
            for port in func.output_order
        }
        irest = func.type_scheme.body.inputs.rest
        orest = func.type_scheme.body.outputs.rest
        irest = f", #: {irest}" if irest else ""
        orest = f", #: {orest}" if orest else ""
        print(
            f"{chalk.bold.blue(name)}({_arg_str(inputs)}{irest})"
            f" -> ({_arg_str(outputs)}{orest})"
        )
        if func.docs:
            print(chalk.green(func.docs))
        print()


@cli.command()
@click.pass_context
@click.option("--namespace", type=str)
@click.option("--function", type=str)
@coro
async def signature(
    ctx: click.Context, namespace: Optional[str], function: Optional[str]
):
    async with ctx.obj["client_manager"] as client:
        client = cast(RuntimeClient, client)
        label = ctx.obj["runtime_label"]
        print(chalk.bold(f"Functions available on runtime: {label}"))
        print()
        sig = await client.get_signature()
        namespaces = [namespace] if namespace else list(sig.keys())

        for namespace in namespaces:
            _print_namespace(sig, namespace, function)
            print()
