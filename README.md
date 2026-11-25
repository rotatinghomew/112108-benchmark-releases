# 图像基准库的隔离组与重新发版

站点合作的图像基准交换记录，覆盖采集、派生、材料同源及批次领用。图像内容以不透明标记表示，
系统只处理交付方提供的指纹与标记，不接触图像像素。

## 资料

来源记录使用 UTF-8 JSON Lines，每行有交付次序 `step`、记录类型 `kind` 和载荷 `body`。
按交付次序阅读 `data/exchanges.jsonl`，字段说明见 `data/字典.md`。

## 系统组成

`benchbank/` 是供不同团队反复领用的**基准仓**（不是一份选好图片的名单）。站点、轮次、
用途与分组名全部来自数据，没有任何写死取值，可接续登记新的交付与领用事实：

| 模块 | 职责 |
| --- | --- |
| `benchbank/load.py` | 交换件形状校验与解析（step 必须单调、字段齐全） |
| `benchbank/repo.py` | 任意交付时点的快照、隔离组、资格、暴露、污染编年史、发布清单、单图追溯 |
| `benchbank/planner.py` | 同轮修订发布（延续旧分配 + 补齐任务单）与覆盖缺口报告 |
| `benchbank/append.py` | 追加登记新事实，step 自动接续，写后立即回读校验 |
| `benchbank/cli.py` | 领用方与维护方的命令行视图 |

## 规则如何落地

- **身份与指纹分离**：身份是 `站点/采集号`，`bytes_id` 只是内容指纹。同字节不并组
  （`bytes-audit` 专门列出同字节不同身份），不同站的本地编号天然不会互相覆盖。
- **隔离组**：有效 `derived`（衍生品→父品）与 `peer`（参考材料同源）边都把两端图像
  并入同一隔离组，同组不跨训练/调参/盲测分配。组没有永久名字，键由当前成员决定，
  证据订正拆开后组键自然变化。
- **证据效力**：边的两端图像都已登记且证据未被 `alias` 撤回时才有效；父图未登记的
  派生图处于 `pending`，不能列为独立候选，父图补登记后自动解禁。
- **两种撤回的区别**：
  - `alias` 撤回的是证据——边消失、两端图像保留，历史暴露与污染记录不消失；
  - `retire` 撤回的是采集——图像退出分发但留痕，失效**只沿派生依赖向下传播**，
    仅靠 `peer` 相连的图像不因此失格。
- **用途资格**：站点许可 `use` 与标注状态 `marked` 同时满足才计入某用途的可标注图。
- **盲测暴露锁**：盲测方实际领到的图，按当前能确认的同组关系整组锁定，同轮不得转给
  训练/调参；换分组名不改变暴露事实。证据经授权订正拆开后，从未在盲测发行中暴露的
  拆出部分不再被扣住（暴露以实际领到的图及当前同组关系为准）。
- **污染留痕**：同轮不同用途的领用一旦在有效关系下同组，即登记一次盲测比较失效，
  并给出证据链；之后证据撤回，登记仍保留——已经发过的成绩不可再对比。
- **发布版本化**：领用方可固定任一发布号读取当时各用途清单与图像身份；同轮出现后续
  发布后，原包标记为 `claimable: false`，但仍可解释当时发了哪些图。
- **修订原则**：优先延续不受影响的旧用途分配，仅摘除失效/失格/与暴露冲突的部分并
  说明原因，再在任务单的站点范围内补齐最低覆盖；无法满足时通过 `blocked_commitments`
  点名被堵住的承诺（最低数量或站点范围），不偷偷降级。

## 命令行用法

```sh
# 维护方：当前隔离组、单图追溯、污染编年史、同字节审计
python3 -m benchbank.cli groups
python3 -m benchbank.cli image a/12
python3 -m benchbank.cli contamination
python3 -m benchbank.cli bytes-audit

# 领用方：固定发布号读取该版清单与图像身份（可加 --step 回放历史时点）
python3 -m benchbank.cli manifest issued-7
python3 -m benchbank.cli releases

# 修订同轮发布与覆盖缺口（下一轮需要补哪类合格图）
python3 -m benchbank.cli revise r7
python3 -m benchbank.cli gap r7

# 接续登记：stdin 给 JSONL，同一步追加，step 自动接续
cat <<'EOF' | python3 -m benchbank.cli --data data/exchanges.jsonl append
{"kind":"image","body":{"id":"c/7","bytes_id":"zc","use":["blind"],"marked":true}}
EOF
```

## 作为库使用

```python
from benchbank import Ledger
from benchbank.planner import revise_release, coverage_gap

ledger = Ledger.from_path("data/exchanges.jsonl")
snap = ledger.snapshot()                 # 默认最新交付时点；也可 snapshot(step=3)
snap.group_members("a/12")               # 当前隔离组成员
snap.eligibility("b/19")                 # 资格与原因码（retired / retired-dependency / pending-parent）
ledger.contamination_log()               # 盲测比较失效编年史（含证据链）
revise_release(ledger, "r7")             # 修订发布：延续、摘除原因、补齐、堵点
coverage_gap(ledger, "r7")               # 各用途合格供给、缺口与被堵图像
```

## 校验原件与系统测试

使用 Python 3.11，在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

`tests/test_exchanges.py` 只校验来源交换件本身；`tests/test_repo.py` 与
`tests/test_planner.py` 覆盖隔离组、撤回传播、未决父图、暴露锁、拆分解锁、
承诺堵塞与追加登记。处理来自不可信目录的资料时，建议用隔离模式并显式指定本仓路径：

```sh
python3 -I -c "import sys; sys.path.insert(0, '$PWD'); \
import unittest; r=unittest.TextTestRunner().run(unittest.TestLoader().discover('tests')); \
raise SystemExit(0 if r.wasSuccessful() else 1)"
```
