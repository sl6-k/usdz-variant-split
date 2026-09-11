#!/usr/bin/env python3
"""交叉导出完整产品的 USD 变体组合（如颜色 × 姿态），不拆分网格。

python3 split_usdz.py phone.usdz --list
python3 split_usdz.py phone.usdz -o result
python3 split_usdz.py phone.usdz -o result --variant-sets Color Pose

默认调用 Blender 4.2.x 自带 Python/OpenUSD；AVIF 贴图自动转 PNG。
仅遇到 AVIF 时，需要启动本脚本的 Python 安装支持 AVIF 的 Pillow。
--runtime blender 可改为启动 Blender 后台进程执行相同的 USD 操作。
"""

import argparse
import io
import itertools
import json
import math
from pathlib import Path, PurePosixPath
import re
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile


def parser():
    result = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    result.add_argument("input", type=Path, help="输入 USDZ 或 USD 文件")
    result.add_argument("-o", "--output", type=Path, help="新的或空的输出目录")
    result.add_argument("--list", action="store_true", help="仅列出当前可见的变体集及选项")
    result.add_argument("--prim", help="变体所在节点，例如 /Phone；不是导出范围")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--variant-set", help="仅遍历这个变体集，兼容旧版用法")
    mode.add_argument("--variant-sets", nargs="+", help="交叉遍历指定变体集，例如 Color Pose")
    mode.add_argument(
        "--current-only", action="store_true",
        help="只导出当前完整模型；可用于修复已经拆出的、没有变体的 USDZ",
    )
    mode.add_argument(
        "--all-variant-sets", action="store_true",
        help="同时把联动的内部变体作为独立维度强制交叉，可能覆盖顶层选项的效果",
    )
    result.add_argument("--variants", nargs="+", help="只有一个遍历维度时，限制导出的选项")
    result.add_argument("--dry-run", action="store_true", help="仅显示组合计划，不写入文件")
    result.add_argument(
        "--select", action="append", default=[], metavar="PATH:SET=VALUE",
        help="先固定其他变体，可重复并按顺序执行，例如 /Phone:model=Pro",
    )
    result.add_argument("--blender", help="Blender 可执行文件路径；也支持 BLENDER_BIN")
    result.add_argument(
        "--image-python", help="解码 AVIF 的 Python；默认使用启动本脚本的 Python",
    )
    result.add_argument(
        "--keep-avif", action="store_true", help="保留原始 AVIF，跳过 PNG 兼容转换",
    )
    result.add_argument(
        "--runtime", choices=("bundled", "blender"), default="bundled",
        help="bundled=Blender 自带 Python（默认）；blender=Blender 后台进程",
    )
    result.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--_version", default="unknown", help=argparse.SUPPRESS)
    return result


def validate(args):
    args.input = args.input.expanduser().resolve()
    if not args.input.is_file():
        raise ValueError("输入文件不存在：{}".format(args.input))
    if args.input.suffix.lower() not in {".usdz", ".usd", ".usda", ".usdc"}:
        raise ValueError("输入必须是 USDZ 或 USD 文件")
    if args.current_only and args.variants:
        raise ValueError("--current-only 不能与 --variants 同时使用")
    if not args.list and not args.dry_run and args.output is None:
        raise ValueError("导出时需要 -o 输出目录；查看选项或计划可用 --list / --dry-run")
    if args.output is not None:
        args.output = args.output.expanduser().resolve()
        if not args.list and not args.dry_run and args.output.exists():
            if not args.output.is_dir() or any(args.output.iterdir()):
                raise ValueError("输出目录必须为空，请换一个新目录：{}".format(args.output))


def find_blender(explicit):
    requested = explicit or os.environ.get("BLENDER_BIN")
    candidates = [requested] if requested else [
        shutil.which("blender"),
        "/Applications/Blender.app/Contents/MacOS/blender",
        str(Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Blender Foundation/Blender 4.2/blender.exe"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.suffix.lower() == ".app":
            path = path / "Contents/MacOS/blender"
        if path.is_file():
            return path.resolve()
        found = shutil.which(str(candidate))
        if found:
            return Path(found).resolve()
    raise ValueError("找不到 Blender，请用 --blender 指定可执行文件路径")


def bundled_python(executable):
    directories = [
        executable.parent.parent / "Resources/4.2/python/bin",  # macOS
        executable.parent / "4.2/python/bin",  # Windows / Linux
    ]
    for directory in directories:
        for name in ("python3.11", "python.exe", "python3", "python"):
            path = directory / name
            if path.is_file():
                return path
    raise ValueError("未找到 Blender 自带 Python，可使用 --runtime blender")


def launch(args, argv):
    executable = find_blender(args.blender)
    version_result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True,
        errors="replace", check=False,
    )
    match = re.search(r"Blender (\d+\.\d+\.\d+)", version_result.stdout)
    if version_result.returncode or not match:
        raise RuntimeError("无法读取 Blender 版本：{}".format(version_result.stderr.strip()))
    version = match.group(1)
    if not version.startswith("4.2."):
        raise ValueError("本脚本面向 Blender 4.2.x，当前版本为 " + version)
    script = str(Path(__file__).resolve())
    if args.runtime == "bundled":
        command = [str(bundled_python(executable)), "-I", "-B", script]
    else:
        command = [str(executable), "--background", "--factory-startup",
                   "--disable-autoexec", "--python-exit-code", "1",
                   "--python", script, "--"]
    command += ["--_worker", "--_version", version,
                "--image-python", args.image_python or sys.executable] + argv
    print("使用 Blender {} / {}，保留完整模型。".format(version, args.runtime), flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise RuntimeError("导出进程失败（退出码 {}），请查看上方日志".format(result.returncode))
    if not args.list and not args.dry_run:
        if not (args.output / "manifest.json").is_file():
            raise RuntimeError("没有生成完成清单，任务未完成")
        print("完成：{}".format(args.output), flush=True)


def unpack_input(source, temporary):
    """先展开 USDZ，使纹理成为普通文件，避免输出再次嵌套整个源 USDZ。"""
    if source.suffix.lower() != ".usdz":
        return source
    directory = temporary / "source"
    directory.mkdir()
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if not entries or PurePosixPath(entries[0].filename).suffix.lower() not in {
            ".usd", ".usda", ".usdc"
        }:
            raise ValueError("USDZ 的首个文件必须是 USD 根图层")
        # 全部路径检查通过后再解包，不允许归档成员写到临时目录外。
        for entry in entries:
            normalized = entry.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or ":" in normalized:
                raise ValueError("USDZ 中包含不安全的成员路径：" + entry.filename)
            if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("不支持 USDZ 中的符号链接：" + entry.filename)
        archive.extractall(directory)
        return directory / entries[0].filename


def apply_selection(stage, path, set_name, value):
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.GetVariantSets().HasVariantSet(set_name):
        raise ValueError("找不到变体集 {}:{}；使用 --list 查看".format(path, set_name))
    variant_set = prim.GetVariantSet(set_name)
    options = variant_set.GetVariantNames()
    if value not in options:
        raise ValueError("{}:{} 没有选项 {}；可选：{}".format(path, set_name, value, options))
    if not variant_set.SetVariantSelection(value) or variant_set.GetVariantSelection() != value:
        raise RuntimeError("切换变体失败：{}:{}={}".format(path, set_name, value))


def parse_selection(text):
    target, separator, value = text.partition("=")
    path, colon, set_name = target.rpartition(":")
    if not separator or not colon or not path.startswith("/") or not set_name or not value:
        raise ValueError("--select 格式应为 /Phone:model=Pro，实际为：" + text)
    return path, set_name, value


def open_stage(source, selections):
    from pxr import Sdf, Usd

    root = Sdf.Layer.FindOrOpen(str(source))
    if not root:
        raise ValueError("无法读取 USD 根图层：" + str(source))
    # 在独立 session layer 写入选择，不修改或保存源图层。
    session = Sdf.Layer.CreateAnonymous("variant_selection.usda")
    stage = Usd.Stage.Open(root, session, load=Usd.Stage.LoadAll)
    if not stage:
        raise RuntimeError("无法打开 USD 场景")
    stage.SetEditTarget(session)
    for path, set_name, value in selections:
        apply_selection(stage, path, set_name, value)
    return stage


def list_variant_sets(stage):
    records = []
    for prim in stage.Traverse():
        for name in prim.GetVariantSets().GetNames():
            variant_set = prim.GetVariantSet(name)
            records.append({"prim": str(prim.GetPath()), "variant_set": name,
                            "selection": variant_set.GetVariantSelection(),
                            "variants": variant_set.GetVariantNames()})
    return records


def driven_variant_sets(stage):
    """找出顶层变体显式选择的子节点变体，避免把内部开关重复做笛卡尔积。

    从各个 variant 的 PrimSpec 读取 variantSelections；不按随机名称猜测。
    使用相对路径映射到当前场景，兼容引用图层的不同根节点名称。
    同节点的其他变体仍可作为独立维度；仅自动排除被控制的后代节点变体。
    """
    driven = set()

    def visit(spec, anchor, scene_prim):
        path = spec.path.StripAllVariantSelections()
        relative = path.MakeRelativePath(anchor)
        target = scene_prim.GetPath() if path == anchor else scene_prim.GetPath().AppendPath(relative)
        if target != scene_prim.GetPath():
            for name in spec.variantSelections.keys():
                driven.add((str(target), name))
        for child in spec.nameChildren.values():
            visit(child, anchor, scene_prim)
        for variant_set in spec.variantSets.values():
            for variant in variant_set.variants.values():
                visit(variant.primSpec, anchor, scene_prim)

    for prim in stage.Traverse():
        for spec in prim.GetPrimStack():
            anchor = spec.path.StripAllVariantSelections()
            for variant_set in spec.variantSets.values():
                for variant in variant_set.variants.values():
                    visit(variant.primSpec, anchor, prim)
    return driven


def show_sets(records):
    for record in records:
        print("{}  [{}]  当前={}  可选={}".format(
            record["prim"], record["variant_set"], record["selection"] or "未选择",
            ", ".join(record["variants"])), flush=True)


def choose_set(records, args):
    matches = [item for item in records
               if (args.prim is None or item["prim"] == args.prim)
               and (args.variant_set is None or item["variant_set"] == args.variant_set)]
    if not matches:
        raise ValueError("没有匹配的 USD 变体集；用 --list 查看。若配色由外部程序控制，"
                         "需要提供原始模型或配色映射，脚本不会改为拆分网格。")
    if len(matches) > 1 and args.variant_set is None:
        color_sets = [item for item in matches if re.search(
            r"color|colour|finish|appearance|look|material|paint|shading|配色|颜色",
            item["variant_set"], re.IGNORECASE)]
        if len(color_sets) == 1:
            matches = color_sets
    if len(matches) != 1:
        show_sets(matches)
        raise ValueError("存在多个候选变体集，请用 --prim 和 --variant-set 明确指定")
    chosen = matches[0]
    if not chosen["variants"]:
        raise ValueError("该变体集没有可导出的选项")
    return chosen


def choose_sets(stage, records, args, presets):
    key = lambda record: (record["prim"], record["variant_set"])
    fixed = {(path, name) for path, name, _ in presets}
    if args.variant_set:
        chosen = [choose_set(records, args)]
        ignored = []
    else:
        candidates = [record for record in records
                      if args.prim is None or record["prim"] == args.prim]
        if not candidates:
            raise ValueError("未发现匹配的变体集，请先用 --list 查看实际节点和名称")
        if args.variant_sets:
            chosen = []
            for name in dict.fromkeys(args.variant_sets):
                matches = [record for record in candidates if record["variant_set"] == name]
                if len(matches) != 1:
                    raise ValueError("变体集 {} 匹配到 {} 个节点；用 --list 查看并用 --prim 限定"
                                     .format(name, len(matches)))
                chosen.extend(matches)
            ignored = []
        else:
            driven = set() if args.all_variant_sets else driven_variant_sets(stage)
            ignored = [record for record in candidates if key(record) in driven]
            chosen = [record for record in candidates if key(record) not in driven
                      and key(record) not in fixed]
    if (args.variant_set or args.variant_sets) and any(key(record) in fixed for record in chosen):
        raise ValueError("--select 固定的变体集不能同时指定为遍历维度")
    if args.variants and len(chosen) != 1:
        raise ValueError("--variants 只适用于一个遍历维度；请同时指定 --variant-set")
    options = []
    for record in chosen:
        values = list(dict.fromkeys(args.variants or record["variants"]))
        invalid = [value for value in values if value not in record["variants"]]
        if not values or invalid:
            raise ValueError("变体集 {} 的选项无效：{}；可选：{}"
                             .format(record["variant_set"], invalid, record["variants"]))
        options.append(values)
    return chosen, options, ignored


def verify_selections(stage, selections):
    # 后面的选择可能改变前面变体的可用性，不把不成立的组合标为成功。
    for path, name, value in selections:
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.GetVariantSets().HasVariantSet(name):
            raise ValueError("组合不成立：变体集 {}:{} 在当前选择下不可用".format(path, name))
        variant_set = prim.GetVariantSet(name)
        if value not in variant_set.GetVariantNames() or variant_set.GetVariantSelection() != value:
            raise ValueError("组合不成立：{}:{}={} 未生效".format(path, name, value))


def safe_name(text):
    cleaned = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")
    return cleaned.encode("utf-8")[:90].decode("utf-8", errors="ignore") or "variant"


def check_dependencies(path):
    from pxr import UsdUtils

    layers, assets, unresolved = UsdUtils.ComputeAllDependencies(str(path))
    if unresolved:
        raise RuntimeError("模型依赖缺失，停止导出：{}".format(
            ", ".join(str(item) for item in unresolved)))
    return layers, assets


def convert_avif_images(request_path):
    """在系统 Python 中执行；Blender 自带 Python 无需安装 Pillow。"""
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "转换 AVIF 需要支持 AVIF 的 Pillow。请在启动脚本的 Python 中运行："
            "python3 -m pip install --upgrade 'Pillow>=11.3'"
        ) from error
    # 兼容已安装 pillow-avif-plugin 的环境；新版本 Pillow 可直接读取 AVIF。
    try:
        import pillow_avif  # noqa: F401
    except ImportError:
        pass
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    results = []
    for task in request["tasks"]:
        source, target = Path(task["input"]), Path(task["output"])
        try:
            image = Image.open(source)
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "无法解码 AVIF {}。请安装支持 AVIF 的 Pillow，或用 --image-python "
                "指定已支持 AVIF 的 Python。详情：{}".format(source.name, error)
            ) from error
        with image:
            if image.format != "AVIF":
                raise ValueError("文件扩展名为 AVIF，但实际内容不是 AVIF：" + source.name)
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("暂不将动画 AVIF 转成单张 PNG：" + source.name)
            image.load()
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            pixels = image.convert(mode)
            options = {}
            if image.info.get("icc_profile"):
                options["icc_profile"] = image.info["icc_profile"]
            target.parent.mkdir(parents=True, exist_ok=True)
            pixels.save(target, format="PNG", **options)
            with Image.open(target) as check:
                check.load()
                if check.size != pixels.size or check.mode != mode or check.tobytes() != pixels.tobytes():
                    raise RuntimeError("PNG 转换后像素校验失败：" + target.name)
            results.append({"output": str(target), "width": pixels.width,
                            "height": pixels.height, "mode": mode})
    Path(request["result"]).write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def asset_filename(path):
    from pxr import Ar

    while Ar.IsPackageRelativePath(path):
        _, path = Ar.SplitPackageRelativePathOuter(path)
    return PurePosixPath(path.replace("\\", "/")).name


def read_packaged_asset(path):
    from pxr import Ar

    outer, member = Ar.SplitPackageRelativePathOuter(path)
    archive_source = outer
    while True:
        with zipfile.ZipFile(archive_source) as archive:
            if not Ar.IsPackageRelativePath(member):
                return archive.read(member)
            nested_archive, member = Ar.SplitPackageRelativePathOuter(member)
            archive_source = io.BytesIO(archive.read(nested_archive))


class TextureConverter:
    """同一贴图只转换一次，修改展平层中全部 asset 引用，覆盖两套材质网络。"""

    def __init__(self, directory, image_python, keep_avif=False):
        self.directory = directory / "converted_textures"
        self.image_python = image_python
        self.keep_avif = keep_avif
        self.outputs = {}
        self.records = {}

    def convert_layer(self, layer):
        from pxr import Ar, UsdUtils

        if self.keep_avif:
            return []
        assets = set()

        def collect(path):
            if asset_filename(path).lower().endswith(".avif"):
                assets.add(path)
            return path

        UsdUtils.ModifyAssetPaths(layer, collect)
        if not assets:
            return []
        self.directory.mkdir(parents=True, exist_ok=True)
        resolved_paths = {}
        pending = {}
        for path in sorted(assets):
            resolved = str(Ar.GetResolver().Resolve(path))
            if not resolved:
                raise RuntimeError("AVIF 资源无法解析：" + path)
            resolved_paths[path] = resolved
            if resolved in self.outputs or resolved in pending:
                continue
            index = len(self.outputs) + len(pending) + 1
            stem = "{:04d}_{}".format(index, safe_name(Path(asset_filename(resolved)).stem))
            source = Path(resolved)
            if Ar.IsPackageRelativePath(resolved):
                source = self.directory / (stem + ".avif")
                source.write_bytes(read_packaged_asset(resolved))
            target = self.directory / (stem + ".png")
            pending[resolved] = {"input": str(source), "output": str(target)}
        if pending:
            request_path = self.directory / "conversion_request.json"
            result_path = self.directory / "conversion_result.json"
            # 每批转换先清理旧结果，失败时不能沿用上次的成功记录。
            if result_path.exists():
                result_path.unlink()
            request_path.write_text(
                json.dumps({"tasks": list(pending.values()), "result": str(result_path)},
                           ensure_ascii=False), encoding="utf-8"
            )
            executable = self.image_python or shutil.which("python3") or shutil.which("python")
            if not executable:
                raise RuntimeError("请用 --image-python 指定支持 AVIF 的 Python")
            result = subprocess.run(
                [executable, "-B", str(Path(__file__).resolve()),
                 "--_convert-avif", str(request_path)],
                check=False, capture_output=True, text=True, errors="replace",
            )
            if result.returncode or not result_path.is_file():
                raise RuntimeError("AVIF 转 PNG 失败：\n" + (result.stderr or result.stdout).strip())
            rows = json.loads(result_path.read_text(encoding="utf-8"))
            metadata = {row["output"]: row for row in rows}
            for resolved, task in pending.items():
                row = metadata.get(task["output"])
                if row is None or not Path(task["output"]).is_file():
                    raise RuntimeError("缺少 PNG 转换结果：" + task["output"])
                self.outputs[resolved] = task["output"]
                self.records[resolved] = {
                    "source_asset": asset_filename(resolved),
                    "png_asset": Path(task["output"]).name,
                    "width": row["width"], "height": row["height"], "mode": row["mode"],
                }
                print("贴图转换：{} → {}（{}×{}）".format(
                    asset_filename(resolved), Path(task["output"]).name,
                    row["width"], row["height"]), flush=True)
        replacements = {path: self.outputs[resolved] for path, resolved in resolved_paths.items()}
        UsdUtils.ModifyAssetPaths(layer, lambda path: replacements.get(path, path))
        return [self.records[key] for key in sorted(set(resolved_paths.values()))]


def prim_inventory(stage):
    # TraverseAll 包括内部原型根，Flatten 前后实例原型的名称可能变化，
    # 因此一致性检查在“展平之后”和“打包之后”进行。
    return sorted((str(prim.GetPath()), prim.GetTypeName()) for prim in stage.TraverseAll())


def composition_errors(stage):
    from pxr import Usd

    # Blender 4.2.1 附带的 USD 24.05 未向 Python 暴露 Stage.GetCompositionErrors。
    prims = list(stage.TraverseAll())
    for prototype in stage.GetPrototypes():
        prims.extend(Usd.PrimRange(prototype))
    return [str(error) for prim in prims for error in prim.GetPrimIndex().localErrors]


def export_variant(stage, destination, temporary, texture_converter):
    from pxr import Ar, Sdf, Usd, UsdUtils

    errors = composition_errors(stage)
    if errors:
        raise RuntimeError("USD 场景组合失败：{}".format(errors))
    flat_path = temporary / (destination.stem + ".usdc")
    # Flatten 固定当前变体组合，不对网格进行几何拆分。
    flat = stage.Flatten(False)
    if not flat:
        raise RuntimeError("固定当前配色失败")
    with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
        converted = texture_converter.convert_layer(flat)
    if not flat.Export(str(flat_path)):
        raise RuntimeError("固定当前配色失败")
    check_dependencies(flat_path)
    expected = Usd.Stage.Open(str(flat_path))
    if not expected:
        raise RuntimeError("无法重新读取配色模型")
    expected_inventory = prim_inventory(expected)
    # 释放中间场景；之后用保存的节点清单校验打包结果。
    expected = None
    flat = None
    temporary_package = temporary / destination.name
    if not UsdUtils.CreateNewUsdzPackage(
        Sdf.AssetPath(str(flat_path)), str(temporary_package), "model.usdc"
    ):
        raise RuntimeError("USDZ 打包失败：" + destination.name)
    # 真实重新打开包、检查依赖与节点，成功后才移入交付目录。
    with zipfile.ZipFile(temporary_package) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("USDZ 归档校验失败")
    packaged = Usd.Stage.Open(str(temporary_package))
    if not packaged or composition_errors(packaged):
        raise RuntimeError("导出的 USDZ 无法完整加载")
    _, assets = check_dependencies(temporary_package)
    if not texture_converter.keep_avif and any(
        asset_filename(str(asset)).lower().endswith(".avif") for asset in assets
    ):
        raise RuntimeError("输出仍包含 AVIF 引用，贴图兼容转换未完成")
    if expected_inventory != prim_inventory(packaged):
        raise RuntimeError("打包前后模型节点不一致")
    if any(prim.GetVariantSets().GetNames() for prim in packaged.TraverseAll()):
        raise RuntimeError("输出仍含未固定的变体集")
    count = len(prim_inventory(packaged))
    # 只移动已校验包；输出目录事先要求为空，禁止覆盖现有文件。
    if destination.exists():
        raise ValueError("文件已存在：" + str(destination))
    shutil.move(str(temporary_package), str(destination))
    return count, converted


def worker(args):
    from pxr import Usd

    presets = [parse_selection(value) for value in args.select]
    with tempfile.TemporaryDirectory(prefix="usdz_variants_") as folder:
        temporary = Path(folder)
        source = unpack_input(args.input, temporary)
        stage = open_stage(source, presets)
        records = list_variant_sets(stage)
        if args.list:
            if records:
                show_sets(records)
            else:
                print("当前场景未发现 USD Variant Sets。颜色切换可能由外部程序或节点显隐控制。")
            return
        if args.current_only:
            chosen, options, ignored = [], [], []
        else:
            chosen, options, ignored = choose_sets(stage, records, args, presets)
        total = math.prod(len(values) for values in options)
        print("交叉导出维度：", flush=True)
        show_sets(chosen)
        for record in ignored:
            print("跟随上层联动：{} [{}]".format(record["prim"], record["variant_set"]), flush=True)
        print("共 {} 个完整模型组合。".format(total), flush=True)
        if not args.dry_run:
            args.output.mkdir(parents=True, exist_ok=True)
        exported = []
        texture_converter = TextureConverter(temporary, args.image_python, args.keep_avif)
        # product 对空维度产生一次组合：所有维度已用 --select 固定时导出一份。
        for index, values in enumerate(itertools.product(*options), 1):
            selections = [(record["prim"], record["variant_set"], value)
                          for record, value in zip(chosen, values)]
            # 每个组合使用独立 session，保持原文件不变且避免前一组合状态残留。
            selected = open_stage(source, presets + selections)
            verify_selections(selected, presets + selections)
            label = "__".join("{}-{}".format(name, value)
                              for _, name, value in selections) or "selected"
            filename = "{:02d}_{}_{}.usdz".format(index, safe_name(args.input.stem), safe_name(label))
            if args.current_only:
                filename = args.input.stem + ".usdz"
            print("[{}/{}] {}".format(index, total, filename), flush=True)
            if args.dry_run:
                continue
            count, converted = export_variant(
                selected, args.output / filename, temporary, texture_converter
            )
            exported.append({
                "variant": values[0] if len(values) == 1 else None,
                "selections": [{"prim": path, "variant_set": name, "variant": value}
                               for path, name, value in selections],
                "file": filename, "prim_count": count, "converted_textures": converted,
            })
        if args.dry_run:
            return
        manifest = {
            "input": str(args.input), "blender_version": args._version,
            "usd_version": ".".join(map(str, Usd.GetVersion())), "runtime": args.runtime,
            "mode": "current" if args.current_only else "cartesian", "combination_count": total,
            "texture_compatibility": "original" if args.keep_avif else "avif-to-png",
            "converted_texture_count": len(texture_converter.outputs),
            "prim": chosen[0]["prim"] if len(chosen) == 1 else None,
            "variant_set": chosen[0]["variant_set"] if len(chosen) == 1 else None,
            "variant_sets": [{**record, "variants": values}
                             for record, values in zip(chosen, options)],
            "linked_variant_sets": ignored, "presets": args.select, "exports": exported,
            "notes": ["每个文件包含当前变体组合的完整 USD 场景，不拆分网格。",
                      "原文件不变；输出固定所有当前已选变体，保留当前组合的时间采样。"],
        }
        (args.output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--_convert-avif":
        try:
            convert_avif_images(sys.argv[2])
            return 0
        except Exception as error:
            print("图片转换错误：{}".format(error), file=sys.stderr)
            return 1
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    args = parser().parse_args(argv)
    try:
        validate(args)
        if args._worker:
            worker(args)
        else:
            launch(args, argv)
        return 0
    except Exception as error:
        if args._worker:
            # Blender 用 --python-exit-code 把异常转为非零退出码。
            raise
        print("错误：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Blender 中正常返回，不抛出可能被当作脚本错误的 SystemExit(0)。
    if "bpy" in sys.modules and "--_worker" in sys.argv:
        main()
    else:
        sys.exit(main())
