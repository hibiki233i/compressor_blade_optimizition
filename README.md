# 离心叶轮主叶片形状优化

用真实 CFD 结果驱动的主动学习，优化离心叶轮**主叶片平均线**的形状，目标是同时提高**效率**（`Efficiency`）和**质量流量**（`MassFlow`）。

程序每次挑出几个最有希望的叶片形状，自动跑完 CFturbo 建模 → TurboGrid 划网格 → CFX 求解 → 读取结果，再用新结果更新代理模型、挑下一批。最终给出的 Pareto 前沿只来自真正算成功的 CFD，不使用模型预测值。

仓库提供命令行和桌面界面两种用法，界面只是命令行的"遥控器"和结果查看器，背后跑的是同一套程序。

## 工作原理

```text
从基准叶型出发，生成 12 个角度偏移量（当前只搜索其中 7 个）
        ↓
检查几何约束、排除重复设计
        ↓
CFturbo 改叶型 → TurboGrid 生成网格 → CFX 求解并后处理
        ↓
结果写入 training_data.csv（成功和失败都记录）
        ↓
用成功样本训练代理模型 → 挑下一批候选 → 循环
        ↓
从全部成功样本中导出 Pareto 前沿
```

设计变量是相对基准模板的偏移量：hub 和 shroud 平均线各 5 个 beta 角、各 1 个 theta 堆叠角，共 12 个。当前只搜索其中 7 个，其余 5 个固定。`PressureRatio`、`totalpressureratio` 和 `Power` 会记录下来供人工查看，但不参与优化。当前模板没有分流叶片，所以也没有分流叶片变量。

为什么是双目标、为什么只搜 7 个变量，见 [优化流程 · 关键设计决策](docs/optimization.md#9-关键设计决策)。

## 准备环境

**Python 部分**（任何系统都能用来看结果、跑测试）：

```powershell
python -m pip install numpy pandas
python -m pip install -r requirements-gui.txt   # 桌面界面需要 PySide6
```

需要 Python 3.10 以上。SciPy 和 scikit-learn 可选，有 scikit-learn 时使用 GP/Kriging 代理模型，没有时自动换成精度稍低的 RBF 模型。

**真实 CFD 部分**只能在 Windows 上运行，需要 PowerShell 7、CFturbo、ANSYS TurboGrid 和 ANSYS CFX，以及仓库里没有的 `Templates/` 模板文件夹。

**本机路径**不写进仓库。`blade_shape_config.json` 里的路径都留空，程序从同目录下的 `blade_shape_local.ini` 读取。第一次使用时复制模板，再按本机实际位置修改：

```powershell
Copy-Item blade_shape_local.ini.example blade_shape_local.ini
```

模板里的路径就是原来写在 JSON 里的值。只要字符串保持一致，旧的运行记录就能照常续跑。如果 JSON 里某个路径写了非空值，以 JSON 为准。界面「项目设置」保存时，只把改动过的路径写回这个 INI。

CFX 和 TurboGrid 的路径（`cfx_bin_dir`、`turbogrid_exe`）也可以都不填，程序会根据 ANSYS 安装时设置的环境变量 `AWP_ROOT251` 等自动推导。本机只装了一个 ANSYS 版本时自动采用该版本；装了多个版本时，需要在 INI 里写 `[ansys] version = 251` 指定，以免新装一个版本后悄悄换掉求解器。

如果 INI 好像没生效，可以用下面的命令查看每个路径实际取的值和来源，它也会提示 INI 是否被 Windows 存成了 `.ini.txt`。这条命令只读，不改任何文件：

```powershell
python blade_shape_local_config.py blade_shape_config.json
```

## 快速上手

### 用桌面界面

```powershell
python -m blade_gui
```

在 Windows 上也可以双击 `run_gui.bat`。第一次打开需要在左侧栏选择配置文件，之后会记住。操作说明见 [桌面界面](docs/gui.md)。

### 用命令行

所有命令都在仓库根目录执行，`--config` 写在子命令后面。

```powershell
# 1. 只检查候选生成和几何规则，不调用任何工程软件
python blade_shape_active_learning.py write-candidate --config blade_shape_config.json --index 0 --dry-run --offline

# 2. 在 Windows 工程机上，先只跑 1 个真实 CFD 确认整条链路通畅
python blade_shape_active_learning.py run --config blade_shape_config.json --initial-samples 1 --iterations 0 --max-new-cfd 1

# 3. 已有数据时继续优化：再跑 2 轮、每轮 3 个点，最多新增 6 个 CFD
python blade_shape_active_learning.py run --config blade_shape_config.json --resume --iterations 2 --batch-size 3 --max-new-cfd 6
```

只要输出目录里已经有数据，`run` 就必须加 `--resume`，以免误覆盖。`--max-new-cfd` 用来限制这次最多新增多少个 CFD，建议每次都写上。

## 输出文件

输出目录由配置中的 `paths.output_dir` 决定，通常写在 `blade_shape_local.ini` 里。

| 文件 | 内容 |
| --- | --- |
| `training_data.csv` | 每一次 CFD 尝试：设计变量、性能指标、成功/失败状态和失败阶段 |
| `pareto_front.csv` | 考虑工程容差后的 Pareto 前沿（日常看这个） |
| `pareto_front_strict.csv` | 严格数学意义上的 Pareto 前沿，用于对照 |
| `active_learning_diagnostics.csv` | 每个主动学习点被选中的原因、事先预测值与真实值的对比 |
| `iteration_summary.csv` | 本次运行逐个尝试的简要记录 |
| `cases/case_XXXXXX/` | 每个算例的输入、生成文件和各阶段日志 |

> 注意：`training_data.csv` 里的 `MassFlow` 数值实际相当于 g/s，并不是 kg/s，详见 [数据单位](docs/optimization.md#数据单位)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [优化流程](docs/optimization.md) | 变量与目标、怎么选点、运行与续跑、CFD 收敛判定、边界实验、设计决策 |
| [桌面界面](docs/gui.md) | 界面各页面的用途和操作 |
| [验证与诊断](docs/validation.md) | 展向攻角验证、进口角敏感性 CFD、熵增与流向角诊断 |
| [AGENTS.md](AGENTS.md) | 给 AI 编程助手的协作规则（人也可以参考其中的代码结构表） |

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试只用合成数据和替身程序，不调用任何工程软件。没装 PySide6 时，界面测试会自动跳过。在 macOS/Linux 上测试通过，只能说明 Python 逻辑没问题；真实工程链仍需在 Windows 上单独跑一个算例确认。`tests/fixtures/` 里的模板是合成的，不能用于工程计算。
