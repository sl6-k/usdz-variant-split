# 模型配色变体导出（Blender 4.2.1）

这版 `split_usdz.py` 已替换之前的网格拆分逻辑：将同一模型的配色分别导出，每个 USDZ 都包含完整模型。不会按材质、面或连通块拆开细小网格。

适用前提：配色选择保存在输入 USDZ 的 **USD Variant Sets（变体集）** 中。变体集也可以控制不同型号或外形，脚本同样可以逐项导出整套模型。如果网页/App 的换色按钮只是运行时替换材质，配色选项可能不在 USDZ 内，此时需要原文件和配色映射才能确定处理方式。

## 1. 查看可切换的配色

把脚本下载到本地，在脚本所在目录运行：

```bash
python3 split_usdz.py model.usdz --list
```

示例输出（实际节点路径和名称以你的文件为准）：

```text
/Model  [color]  当前=red  可选=black, blue, red
```

这里 `/Model` 是携带变体的节点，`color` 是变体集，`black/blue/red` 是选项。

## 2. 每个配色导出一个完整模型

```bash
python3 split_usdz.py model.usdz -o result \
  --prim /Model --variant-set color
```

如果仅有一个候选变体集，或者多个候选中仅有一个名称明显与配色相关，也可自动选择：

```bash
python3 split_usdz.py model.usdz -o result
```

多个可能的配色变体集会要求明确指定，不会把所有变体组合自动遍历。名称判断只用于便捷选择；需要确定控制对象时，请明确传入 `--prim` 和 `--variant-set`。

输出示例：

```text
result/
  01_model_black.usdz     # 完整黑色手机
  02_model_blue.usdz      # 完整蓝色手机
  03_model_red.usdz       # 完整红色手机
  manifest.json          # 来源、版本、选项与文件对应关系
```

仅导出指定配色：

```bash
python3 split_usdz.py model.usdz -o selected_colors \
  --prim /Model --variant-set color --variants black blue
```

`--prim` 只用于定位变体，不裁剪导出范围。每个输出包含当前配色下的完整 USD 场景，共用的屏幕、摄像头、按键等会一并保留。

## 指定 Blender 路径

macOS：

```bash
python3 split_usdz.py model.usdz -o result \
  --blender "/Applications/Blender.app/Contents/MacOS/blender" \
  --prim /Model --variant-set color
```

Windows：

```powershell
py -3 split_usdz.py "D:\models\model.usdz" -o "D:\models\result" --blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe" --prim /Phone --variant-set color
```

Linux：

```bash
python3 split_usdz.py model.usdz -o result --blender /path/to/blender
```

也支持 `PATH` 或 `BLENDER_BIN` 环境变量。脚本会检查 Blender 是 4.2.x。

## 型号与配色嵌套

如果先选择 Pro 型号，才会出现该型号的配色集，先固定型号再列出选项：

```bash
python3 split_usdz.py model.usdz --select /Model:model=Pro --list
```

随后按实际输出中的节点和变体集导出：

```bash
python3 split_usdz.py model.usdz -o pro_colors \
  --select /Model:model=Pro --prim /Model --variant-set color
```

`--select` 可重复使用，按照命令行顺序应用。它只固定其他变体，不应与本次要遍历的变体集相同。`--list` 只列出当前选择下可访问的变体，不会递归穷举所有未选分支，也不扫描实例内部不可直接编辑的变体。

## 执行方式与数据保留

默认使用 `--runtime bundled`：系统 Python 启动 **Blender 4.2.1 自带的 Python 和 OpenUSD 库**，直接切换 USD 变体、固定选择并重新打包。无需给系统 Python 安装第三方库，也无需启动 Blender 界面或显卡初始化。

如果希望在 Blender 后台进程里执行同样的 USD 操作，加上：

```bash
python3 split_usdz.py model.usdz -o result --runtime blender
```

两种方式都直接处理 USD，不经过 Blender 网格编辑和材质重建。直接使用 USD 可以在导入 Blender 场景之前选择原始配色，保留材质与纹理引用、坐标变换和当前组合中的时间采样。

处理流程为：临时展开源 USDZ → 在独立 session layer 中选择配色 → Flatten 固定当前组合 → 收集依赖并生成新的 USDZ → 重新打开验证。源文件不会被修改；导出的文件固定所有当前已选变体，未选分支不再作为可切换选项保留。Flatten 会解析引用、继承和层组合，实例原型的内部结构可能调整，但不对手机网格做几何拆分。

输出目录必须不存在或为空。若中途失败，可能保留之前已校验成功的配色文件，但不会生成完成清单；重试请换一个空目录。

## 已验证的范围

已在本机 **Blender 4.2.1 自带 Python 3.11 / USD 24.05** 下完成合成模型的端到端验证：列出三种配色，分别导出后在新进程中重新打开；每份都保留机身、屏幕、摄像头三个网格及原始面拓扑，配色标记正确，对应 PNG 贴图内容一致且位于包内，原始 USDZ 的三种变体和默认配色保持不变。

参考：[OpenUSD 变体接口](https://openusd.org/dev/api/class_usd_variant_set.html)、[Flatten 固定当前选择](https://openusd.org/dev/api/class_usd_stage.html)、[USDZ 依赖打包接口](https://openusd.org/dev/api/usdz_package_8h.html)。
