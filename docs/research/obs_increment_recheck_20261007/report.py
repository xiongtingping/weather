"""Build the durable research receipt from verified JSON without extra deps."""
import gzip,hashlib,json,os,subprocess
import numpy as np
from audit import HERE,guard

def main():
    guard();a=json.load(open(HERE/'audit.json'));e=json.load(open(HERE/'experiment.json'));s=json.load(open(HERE/'stress.json'));case=json.load(open(HERE/'case.json'));v=json.load(open(HERE/'verification.json'))
    assert v['passed'] and e['verdict']=='RESEARCH_ONLY_NO_PRODUCTION'
    sat=json.load(gzip.open(HERE/'source_cache/ZUUU_jma_jaxa_himawari_2026-09-01_2026-10-07.json.gz','rt'))['payload']['hourly']
    pred=json.load(gzip.open(HERE/'source_cache/ZUUU_previous_2026-09-01_2026-10-07.json.gz','rt'))['payload']['hourly']
    sv=dict(zip(sat['time'],sat['shortwave_radiation']));fv=dict(zip(pred['time'],pred['shortwave_radiation_previous_day1']))
    cc=[r for r in case['rows'] if r['lag_hours']==1];h=[r['hour'] for r in cc]
    version_table='\n'.join(f"| {k} | {x['rows']} | {x['days']} | {x['first']}–{x['last']} |" for k,x in a['versions'].items())
    score_table=[]
    for label,key in [('辐射，0h 假设','lag0_nll_radiation'),('辐射，1h 假设','lag1_nll_radiation'),('辐射，2h 假设','lag2_nll_radiation'),('辐射＋雨，1h','lag1_nll_joint'),('Brier 拟合，1h','lag1_brier_radiation'),('最高温 MSE 拟合，1h','lag1_physical_mse_ratio')]:
        r=e['runs'][key]['current_v38'];lo,hi=r['ci95']['logscore'];score_table.append(f"| {label} | {r['rows']} | {r['delta']['logscore']:+.5f} [{lo:+.5f}, {hi:+.5f}] | {r['delta']['crps_c']:+.5f} | {r['delta']['brier']:+.5f} |")
    case_table=[]
    for h in (12,13,14,15,16):
        av=sv[f'2026-10-07T{h:02d}:00'];f=fv[f'2026-10-07T{h:02d}:00'];row=next(r for r in cc if r['hour']==h)
        case_table.append(f"| {h}:00 | {f:.1f} | {av:.1f} | {(av/f-1)*100:+.1f}% | {row['baseline_p25']*100:.1f}% → {row['fitted_p25']*100:.1f}% |")
    text=f"""# 实况减预报独立增量：重新检验（2026-10-07）

## 版本与口径

本次研究只读，不改生产代码、配置、artifact、服务或订单。现行链：P0 v27、P0-M v30（实际使用时）、P1 v38。19,618 个旧样本覆盖 48 站、09-01..10-06，共 **{len(a['versions'])} 个 P1 版本**。现行 v38 只有 10-04..10-06 **3 个闭日、1,471 行、127 站日**。P0、P0-M 的逐行确切父链版本分布保存于 audit.json：P0 有 24 个版本；4,601 行直接以 P0 为根，未使用 P0-M，并非缺失报文。

基线是数据库内当时服务的向量，未用现行函数重放旧链；所有统计按 P1 版本分别保存。参数的探索性训练跨旧版本，现行段读数不能当作现行链参数的严格准入依据。实际温度单位、桶边界来自信封 canonical intervals，CRPS 按 °C 距离；本次可验证卫星源的 34 站不含美国 11 个 °F 站，不能声称覆盖 °F 实盘。观察端均为 archive 的 awc METAR，复核有效、首次收到、canonical 提交三种时间。真值按 canonical formal AWC/SETTLEMENT_SOURCE 两车道、修订号排序；训练只取截止前已提交的修订。

预报为 Open-Meteo previous_day1 固定 lead 的 republisher 产品，不能证明逐值发行/到货，更不能证明余下整条曲线来自同一份日前 run。卫星源显式指定 JAXA Himawari、DWD MTG SIS、EUMETSAT LSA SAF，均保留 Open-Meteo aggregator_republisher 身份；**没有历史到货回执或逐值 QA，所以全部为 observational_candidate_not_oos**。0/1/2h 只是延迟压力假设，不是 strict-as-of。10-07 尚未闭日，25°C 仅用于展示该桶，不参与拟合或 OOS。

## 结论

**真实少晒信号存在，亚洲卫星通道可用；当前仍没有达标的独立强似然，生产不接入。** 上次“不成立”的总体断言不能由混合版本、错误支持集和未核收到时间的实验支持。本次也不把 3 个闭日的改善倾向说成稳定成功。

当前 P1 只有 cloud_cover_fraction、precipitation_present 等联合更新特征，没有观测减预报的独立云/雨/辐射项。现行 artifact 在 ZUUU 15 时，固定其他特征，SCT→BKN 的“再升”log-odds 增量为 **+0.0911**（不是概率增加 9.1 个百分点），方向未受物理约束。这里未改该生产项。

## 来源重新验证

- satellite-api 默认值在成都 10-07 的 24 个小时与普通 forecast API **逐值相等**，不能当卫星。此前纽约“真卫星”的说法同样不能由默认接口证明；KLGA 显式卫星源不可用。
- 指定 `models=jma_jaxa_himawari` 后，成都与东京可取得不同于预报的卫星反演 SWR；无需注册 JAXA 账号即可通过 republisher 做研究采集。JAXA 官方用户指南确认其短波产品覆盖 70E–150W、60N–60S，10min/1h，5km。API 不等价于机场辐射仪。
- 34/48 站已捕获 09-01..10-07 历史；14 站没有本提供者的经核实卫星源，明确排除，不补预报。成都另核实 2025-01-01..02 共 48 小时均有卫星 SWR；DWD SIS 该早期探针 48 小时全空，不能把不同源可回溯范围混说。
- previous_day1 的温度、总云、短波、降水在 09-01..10-07 和 2025-01-01..02 探针完整；低云字段分别 888/888、48/48 全空。未把“未报低云”当总云为0，未把 METAR 云量推算成辐射。
- 实况降水只用收到的 METAR “有雨”事实与同小时预报 0mm 的分类差异，不伪造实测雨量；干报文不充当完整小时雨量0。缺匹配低云预报时不创建低云差分。云导致的能量变化通过实际短波进入一次，不再把云/雨/辐射当三份独立证据相乘。

## ZUUU 25°C 复现

表中辐射是整点时段值；概率是对应小时最后一份 P1，用之前两个完整小时的能量差、再假设 1h 延迟更新，二者时间窗不同。系数仅来自 10-06 测试折之前的历史，未用 10-07 拟合。

| 当地小时 | 日前预报 W/m² | 卫星实况 W/m² | 实况差异 | 25°C 概率，服务→候选 |
|---|---:|---:|---:|---:|
{chr(10).join(case_table)}

12 时已出现少晒；不能用“15 时才报 BKN”推断此前没有云的辐射影响。但它也不是云致升温受限的单站因果证明。日前这条外部温度曲线本身的最高为 **24.8°C**，已经接近 25°C；它不是生产六家集合先验对应的同一条预测。因此，拿它“复现25”不能证明修复了生产先验。

历史拟合后的辐射项只把 13 时该桶从 13.2% 提到约 13.7%，**没有提前把25°C纠正为主桶**。手工强度可以提高该站概率，却不能据此上线。

## 探索性前推与不按 LogScore 的对照

独立增量是条件 log-likelihood tilt：ΔR=(实况2h能量−预报2h能量)/预报2h能量，L(k)=exp((βR·ΔR−βrain·预报外雨)·距当前最高温的°C增量)，β∈[0,8]，所有物理方向固定。不添加实况气温的第二份似然。按站日等权，12 个历史事件日后逐日前推，训练真值按最早测试决策的 UTC 截点；评分只应用一次生产下限，排除桶保持0。分别用 NLL、Brier 拟合，另用最高温 MSE 拟合辐射缩放 γ，更新用相对 γ=0 的高温似然比，避免温度重复计数。

下表只列现行 v38 的测试切片（3个事件日），参数由旧链历史训练；95% 区间是事件日块重抽2,000次的描述性区间，**不构成认证**。整窗混合读数和各个旧版本单列保存在 experiment.json。

| 方法 | 现行行数 | ΔLogScore（95%区间） | ΔCRPS °C | ΔBrier |
|---|---:|---:|---:|---:|
{chr(10).join(score_table)}

NLL 拟合的最后辐射系数约 0.17（1/2h）；Brier 拟合在1/2h选0。**最高温 MSE 拟合在全部前推折都选 γ=0**，没有因不采用 LogScore 就得到强修正。这里是所测候选的结果，不是否定所有物理模型。

强度压力测试（2h 假设）：现行段的强度1/2/4/8 ΔLogScore 分别 {s['forced']['1.0']['current_v38']['delta']['logscore']:+.4f} / {s['forced']['2.0']['current_v38']['delta']['logscore']:+.4f} / {s['forced']['4.0']['current_v38']['delta']['logscore']:+.4f} / {s['forced']['8.0']['current_v38']['delta']['logscore']:+.4f}；少晒切片对应 +0.0229/+0.0681/+0.2245/+0.7600。强制加权没有稳定支持。仍是短版本段的压力读数。

## 旧实验修正与验证

旧分数算术复现：站日均值 ΔLogScore −0.010361、ΔBrier −0.007477。该算术一致不证明口径正确：{a['counts']['latest_valid_report_unavailable']} 行的最新有效报文在决策时尚不可用，{a['counts']['feature_diff_received_prefix']} 行物理特征改变，{a['counts']['high_diff_received_prefix']} 行运行最高温改变；19,618 行评分均给已排除桶重新补概率。上游24个P0版本和33个P1版本混合，不能用合并结果作现行强似然的上线或永久否决依据。

独立验证通过：30组标量/向量与反方向压力边界、0强度和0证据精确identity、缺测/夜间拒绝、2°F换算后CRPS距离、全部9,344行2h候选的收到/提交截止与支持集、训练真值截点、来源身份与未来null。生产 P1/宪章相关 **22项测试通过**。未重启服务、未修改 live 配置和 SSOT。

结果以可核对的表格交付。当前系统/仓库/内置 Python 均缺 matplotlib，未生成可选附图；未为绘图更改生产环境。

## 已完成／仍阻断／下一个主线任务

已完成：当前实现核查、旧样本口径修正、34站真实短波候选采集、三种延迟、三种拟合目标、强度和支持集压力测试、ZUUU 当日复现。

仍阻断：现行版本只有3个闭日；训练跨旧链；卫星历史逐值到货/QA缺失；日前预报发行/到货及与生产先验的 exact run 对齐缺失；没有匹配的低云增量或定量雨量。**因此未完成严格OOS准入，明确不改生产。**

下一个主线任务：为指定卫星产品建立真实到货与质量回执，并把差分对齐生产实际采用的预测 run；在现行链严格 as-of 重放或足量现行闭日上重新验证。不能只靠等3天旧训练、或只靠该站25°C成功，替代这一步。

## 复跑与证据

顺序：`audit.py` → `enrich.py` → `sources.py` → `sources.py all` → `experiment.py` → `stress.py` → `case.py` → `verify.py` → `report.py`。纯读生产库；响应有界，RSS 1GiB、单响应2MiB、累计32MiB、输出128MiB、磁盘保底2GiB。

来源：JAXA <https://www.eorc.jaxa.jp/ptree/userguide.html>；Open-Meteo previous runs <https://open-meteo.com/en/docs/previous-runs-api>；源选择代码 <https://github.com/open-meteo/open-meteo/blob/main/Sources/App/Controllers/ForecastapiController.swift>。docs访问403时以成功API探针、官方JAXA指南和源代码核查，不从403推断无数据。

JSON receipt 与缓存保存请求URL、捕获时间、源产品、hash、null与不足；源代码摘要保存hash。raw缓存和audited_rows保留为本次研究必需输入，未进入SSOT。`manifest.json` 记录代码/结果指纹，`verification.json` 记录验证。

### P1 版本分布

| 版本 | 行数 | 事件日数 | 日期 |
|---|---:|---:|---|
{version_table}
"""
    (HERE/'RESULTS.md').write_text(text)
    # Inspectable notebook companion; substantial computations live in the
    # resource-bounded scripts rather than a second implementation.
    notebook=dict(nbformat=4,nbformat_minor=5,metadata=dict(kernelspec=dict(display_name='Python 3',language='python',name='python3')),cells=[dict(cell_type='markdown',metadata={},source=['# 实况增量复核\n','当前数据全部为研究候选，非strict-as-of OOS；见RESULTS.md。\n']),dict(cell_type='code',execution_count=None,outputs=[],metadata={},source=['from pathlib import Path\n','import json\n',f"research = Path({str(HERE)!r})\n","audit = json.loads((research / 'audit.json').read_text())\n","experiment = json.loads((research / 'experiment.json').read_text())\n","audit['versions']['p1_joint_likelihood_v38'], experiment['verdict']\n"]),dict(cell_type='code',execution_count=None,outputs=[],metadata={},source=["case = json.loads((research / 'case.json').read_text())\n","[{k:r[k] for k in ('hour','baseline_p25','fitted_p25','radiation')} for r in case['rows'] if r['lag_hours']==1]\n"])])
    (HERE/'analysis.ipynb').write_text(json.dumps(notebook,ensure_ascii=False,indent=2))
    files=[p for p in HERE.iterdir() if p.suffix in ('.py','.json','.md','.png','.ipynb') and p.name!='manifest.json']
    manifest=dict(production_code_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERE).decode().strip(),research_authority='observational_candidate_not_oos',production_changed=False,files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},audited_rows_sha=a['audited_rows_sha'],source_cache_retained_as_research_input=True)
    (HERE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));print('Report, notebook, manifest saved')

if __name__=='__main__':main()
