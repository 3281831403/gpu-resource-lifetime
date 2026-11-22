# gpu-resource-lifetime

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 图形渲染 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 题面

显存资源接入延迟释放后，偶发野指针崩溃、跑久了句柄还会耗尽：release 在引用计数归零的瞬间就真正释放资源，同一帧里还在被引用的句柄立刻变成野指针；tick 用 frame >= deadline 判定到期，在截止帧提前一帧回收，延迟窗口差一帧，释放时序全错；句柄复用只比较槽位号、不比较代际号，旧句柄会误操作刚被复用的新资源；retain 不刷新最后使用帧，泄漏报告把仍在活跃使用的资源误判成泄漏；leak_report 只按引用计数是否为 0 判断，引用归零但还停在延迟释放队列里的资源被重复报告；cycle_detect 只找 A→A 自环，多节点环（A→B→C→A）全部漏检；validate_registry 不检查延迟队列里的句柄是否还在存活表，队列悬挂永远发现不了；release 不抑制重复释放，引用计数可以降到 −1；create 的槽位号只增不减、从不回收，跑久了句柄空间耗尽；tick 一次只释放一个到期资源，批量到期时回收积压、帧预算被拖垮。难点是"何时真正释放"：引用计数、延迟帧数、句柄代际三件事必须同时正确。lifetime/registry.py 是核心，tests/test_registry.py 15个红。要修的是：把上述十个生命周期缺陷全部修掉，让 15 个测试全部转绿。

## 运行方式

在仓库根目录执行：

```powershell
python -m pytest tests -q
```

初始（未实现）状态必须恰好是 `15 failed`，且没有任何 collection error、import error 或 syntax error：

```text
15 failed in 0.xxs
```

15 个失败全部来自 `lifetime/registry.py` 里的 8 个桩函数：它们都只写契约、不写实现，一律 `raise NotImplementedError`。测试本身是有效的，不允许靠改测试来“通过”。

## 题目属性

| 字段 | 值 |
| --- | --- |
| 领域 | 游戏、图形与多媒体 |
| 子领域 | 图形渲染 |
| 类型 | Bugfix |
| 技术栈 | Python 3, pytest |
| 依赖 | 无外部依赖 |

## 契约速览

- 句柄：`handle = (generation << 32) | slot`，从 1 开始按槽位递增代际；槽位回收后重新分配必须换一个更大的代际号，旧句柄一律失效并 `raise KeyError`。
- 延迟释放：`RELEASE_DELAY_FRAMES = 2`；在帧 `f` 归零时 `deadline = f + 2`，资源在 `f`、`f + 1`、`f + 2` 三帧内仍可 `retain`/使用，只有 `frame > deadline` 的 `tick` 才回收（即最早 `f + 3`）。边界是严格大于，容差为 0。
- 泄漏：`refcount > 0` 且 `frame - last_used_frame > LEAK_AFTER_FRAMES(=600)`，或 `refcount == 0` 却不在延迟队列里；延迟队列中的资源不算泄漏。
- 帧号：全部由调用方显式传入，不读时钟、无随机；`tick` 不允许回退。
- registry 是一个普通 dict，字段布局与资源记录字段写在 `lifetime/registry.py` 的模块 docstring 中；`tests/test_registry.py` 只按该布局构造纯审计用例的状态。

## 文件结构

```text
gpu-resource-lifetime/
├── README.md
├── conftest.py
├── .gitignore
├── lifetime/
│   ├── __init__.py
│   └── registry.py
└── tests/
    └── test_registry.py
```

## tests 不可修改

`tests/` 目录（含 `tests/test_registry.py`）是评测契约，**不可修改、不可删除、不可跳过**。修复只能发生在 `lifetime/registry.py`（以及必要时 `lifetime/__init__.py`），目标是 `python -m pytest tests -q` 从 `15 failed` 变成 `15 passed`。
