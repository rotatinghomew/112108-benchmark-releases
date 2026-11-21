# 图像基准库的隔离组与重新发版

站点合作的图像基准交换记录，覆盖采集、派生、材料同源及批次领用。图像内容以不透明标记表示。

## 资料

来源记录使用 UTF-8 JSON Lines，每行有交付次序 `step`、记录类型 `kind` 和载荷 `body`。按交付次序阅读 `data/exchanges.jsonl`，字段说明见 `data/字典.md`。

## 校验原件

使用 Python 3.11。在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```
