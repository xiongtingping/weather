from audit import *
import csv
s=json.loads((OUT/'summary.json').read_text());cells=list(csv.DictReader((OUT/'station_lead_matrix.csv').open()))
text=f'''# 45 站六全球集合模型：历史覆盖与 OOS 证据核验

熊先生，数据支持按站点、提前量、起报周期继续研究模型权重，但尚不支持直接宣布某个模型是某站的赢家。当前限制并非只有样本少，而是历史样本、当时可知的回执/标签、激活认证三者尚未完全闭合。

评估时点：{s['asof']}；统一事件截止：{s['cutoff']}。覆盖全部 45 站（34 C、11 F），六官方原生产品。小时 lead 表示起报到当地日开始的距离，不等于距市场结算时间，不能把 h48_plus 直接等同于 Proper 的 >36h 下注门。

## 核实结果

- 2,497 个有历史的 exact scope，共 118,269 个完整模型站日例、4,588,938 条成员引用，逐条成员数和哈希关联通过。所有入选成员的 archive query 均早于本轮审计时点。
- 1,864 个 scope 满足 20 train + 8 holdout 的**数量门**。没有把不同起报周期拼起来凑数。
- 513 个 scope 满足本轮更保守的**日前回执候选条件**：8 个留出日在当地日开始前收到；移除当时不可知/后来改值的训练标签及迟到成员后，仍有至少 20 天可训练。这仍不是技能通过或正式 OOS 认证。
- 当前 3,011 个有效单模型 exact activation，全部为 `FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT`，全部 `PRODUCTION_NO_LIVE_ONLY`，`formal_p0_eligible=false`；晋级记录表为空。数量比历史 scope 多，是因季相/有效 scope 和统一闭日历史口径不同，不能用数量差推断异常。

| 模型 | 有历史 scope | 满足 20＋8 数量 | 日前回执候选 | 每 scope 历史日数范围 |
|---|---:|---:|---:|---:|
'''
for r in s['models']:text+=f"| {r['model']} | {r['scopes']} | {r['count_20_8']} | {r['receipt_candidate']} | {r['min_days']}–{r['max_days']} |\n"
text+='''
AIFS、AIGEFS 的 60 多天历史真实存在，但本轮日前回执候选为 0；这不是预测能力差的结论，也不是断言原始官方历史不可获得。它表示现有回执、标签与所选历史留出期在本轮条件下尚不能组成候选。部分回填在事件结束后才采集：全样本只有 47,380/118,269 例的成员 archive query 全部早于当地日终；另外 70,889 例不能用采集时间证明当日实时已知。可核验的官方发布/保留回执如能补充，需逐 scope 重新审查，不能用起报时刻替代。

## 为什么现有 OOS 标签不能直接当严格证书

canonical loader 对 eligible native archive 使用 `native_run_init_utc` 作为回放时点；核对到的报文包则另存真实采集时间。以目前最后 8 天留出划分，在数量达标的范围内：1,499 个 scope 至少包含一条在首个留出起报时尚未知的训练标签；938 个至少包含一条当时已有标签、后来又修订改值的训练日（两数有重叠）。这说明必须重建训练当时的标签版本，而非仅把训练事件日限制在留出日之前。

三个独立回查例子：

| 站点/训练日 | 留出第一起报 | 当时可知标签 | 后来最新标签 | 修订入库 |
|---|---|---:|---:|---|
| KATL / 09-09 | 09-19 00Z | 30.6°C | 31.7°C | 09-24 |
| EGLC / 08-01 | 09-19 00Z | 22°C | 26°C | 09-25 |
| RJTT / 07-31 | 09-19 00Z | 33°C | 35°C | 09-25 |

最终修订真值可用于事后评分；训练则必须使用决策前可知版本。这里没有撤销正式修订、也没有把旧值重新写回生产。

## 哪些站、哪些提前量可以继续

| 起报提前量 | 至少两模型通过数量门的站数 | 至少两模型存在日前回执候选的站数 |
|---|---:|---:|
'''
for lead,r in s['leads'].items():text+=f"| {lead} | {r['count_two']}/45 | {r['receipt_two']}/45 |\n"
text+='''
下表每格为“数量达标模型数 / 日前回执候选模型数”，分母均为六模型；某模型至少一个 exact cycle 满足即计 1，不表示其所有周期通过。具体模型名单在 `station_lead_matrix.csv`，具体起报周期、训练/留出日期、缺口、关联激活见 `exact_scopes.csv`。六模型 independence_family 取当前 registry，不靠机构名称猜测独立性。

| 站点 | h00_12 | h12_24 | h24_48 | h48_plus |
|---|---:|---:|---:|---:|
'''
for station in json.loads((OUT/'manifest.json').read_text())['stations']:
    cc=[next(r for r in cells if r['station']==station and r['lead']==lead) for lead in ['h00_12','h12_24','h24_48','h48_plus']]
    text+='| '+station+' | '+' | '.join(r['count_20_8_n']+' / '+r['receipt_timestamp_candidate_n'] for r in cc)+' |\n'
text+='''
## 对逐站选模型的实际含义

1. 以站点 × 提前量 × 每模型真实起报周期建立候选池；短历史周期不能借用同模型其他周期的评分，缺历史也不等于模型能力差。
2. 优先对 h24_48 做共同日期、严格时点的历史 OOS 重放：45 站均有至少两个模型的日前回执候选。选共同训练和留出日期后重新计门，不能将各模型自己的 8 天留出直接拼接比较。
3. 每折绑定原生成员及实际 arrival/receipt，训练标签按该折训练截止时点取最新可知修订；修订后的最终真值留作事后评价。加入 AIFS/AIGEFS 须先解决历史可用时点证据，不能模拟到货。
4. 再比较单模型与组合的桶概率 Brier/log loss、偏差、区间覆盖；用按日期分块的误差范围判断收益是否稳定。证据薄的站应限制权重偏离、保留至少两个独立 family，不以少量天“选一个冠军”。本轮没有拟合新权重，也未比较模型优劣。

## 验证与限制

全量逐日例重新分组核算 scope、唯一日期、成员数量、20+8 边界；RKPK（C）与 KSFO（F）重新读取结果一致；六模型 × RKPK/KSFO/CYYZ 的 18 组原始包、capture 哈希与 SSOT receipt reference 对照通过；所审计生产代码指纹保持一致。另核查 19/20 train、7/8 holdout、重复日期和时点相等边界。

这是一份覆盖/可知性审计，不是已完成的 strict OOS 证书：全历史原始回执包未逐一核验，历史共同样本技能重放尚未执行；513 是保守条件下可继续核验的范围数，不是可直接上线数。每站使用独立只读事务，评估 as-of 固定但并非全库物理快照；结果保留内容哈希与重复读取验证，无法完全排除审计期间的后补数据影响。

本轮只新增研究文件，没有修改生产代码、参数、服务或 SSOT。

**下一个主线任务：以 h24_48 的 45 站候选池为起点，完成回执绑定、逐折历史标签和共同留出日期的严格重放，再产出逐站模型技能及权重比较。**

复现：依次运行 `audit.py`、`activation.py`、`receipt_samples.py`、`validate.py`、`summarize.py`、`report.py`。audit.py 保留 manifest 时复用冻结时点和已生成站点文件；完整重采应在新的研究目录创建新的 manifest。全部从项目根使用 `PYTHONPATH=. .venv/bin/python <脚本路径>`。
'''
(OUT/'REPORT.md').write_text(text)
print(OUT/'REPORT.md')
