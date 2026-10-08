"""Render the current research state, without promoting partial evidence."""
import json
from rebuild import HERE,old,guard
from fit_storage import load_fit_report


def load(name):
    path=HERE/name
    return json.loads(path.read_text()) if path.exists() else None


def metric(x,index=0):
    if 'mean' not in x:return x['state']
    return f"{x['mean'][index]:+.6f} [{x['ci95'][index][0]:+.6f}, {x['ci95'][index][1]:+.6f}]"


def main():
    guard();fit=load_fit_report(HERE/'restricted_refitted_fit.json');case=load('case_increment_math.json');origin=load('cloud_origin_audit.json');alternates=load('alternative_packets.json');members=load('alternative_member_verification.json');prefix=load('prefix_refitted_rows.json');p0=load('p0_refitted_batch.json');nested=load('nested_batch.json');compiler=load('compiler_batch.json');audit=load('nested_verification.json');fusion=load('fusion_refit.json');facts=load('fusion_facts.json');missing=load('missing_packets.json')
    coverage_betas={mode:[next(x['beta'] for x in r['increments'] if x['variant']==mode+'_6500_coverage_logscore') for r in fit['p1_fit_receipts']] for mode in ('latest','day1')}
    fifteen=next(r for r in case['case'] if r['local_hour']==15)['terms']['latest_6500_coverage_logscore'];noon=case['case'][0]['terms']['latest_6500_base_logscore']
    rows=[]
    for title,key,group in [('最新覆盖差：全部','latest_6500_coverage_logscore','all'),('最新覆盖差：可比云证据','latest_6500_coverage_logscore','known_only'),('最新覆盖差：更多云','latest_6500_coverage_logscore','more'),('提前≥24h覆盖差：全部','day1_6500_coverage_logscore','all'),('提前≥24h覆盖差：可比云证据','day1_6500_coverage_logscore','known_only'),('最新云底差：可比证据','latest_6500_base_logscore','known_only'),('提前≥24h云底差：可比证据','day1_6500_base_logscore','known_only')]:
        x=fit['variants'][key][group];rows.append(f"|{title}|{x.get('rows',0):,}|{x.get('station_days',0)}|{metric(x)}|{metric(x,1)}|")
    flags=f"嵌套候选：{nested['counts'] if nested else '未结束'}；隔离编译：{compiler['counts'] if compiler else '未结束'}；组件全量核验：{audit['counts'].get('COMPONENT_AUDIT_PASS',0) if audit else 0}/48站。"
    text=f'''# 实况减预报云增量：严格重放研究（2026-10-08）

熊先生，**覆盖差有小幅改善信号，强似然尚未获准；云底差的物理方向没有证实。完整现行链检验仍未完成，生产没有修改。** 本报告替代此前“尚未计算任何概率成绩”的阶段记录。

## 可验证终点与阶段复盘

终点：全48站按真实输入到达及输出提交时钟重建现行P0/P0-M/P1；参数只用当时已知的闭日真值；云覆盖差和云底差分别作独立增量似然，完成滚动样本外评分和单位、来源、支持集、强度边界检验；达标才接生产。

已完成：29,602对原文/实况重建；48站原生上游审计；11,156个P0锚点的当前单系统参数重拟（{p0['counts']['ASOF_INPUT_ARITHMETIC_NOT_CURRENT_CHAIN_OOS']:,}成功）；{prefix['counts']['OFFLINE_PREFIX_REBUILT_NOT_PROMOTED']:,}条正式前缀研究锚点；P0-M和P1逐日因果拟合；16日限定样本评分。已恢复区域特征真实fetch时钟、辐射历史version视图和真值修订时钟。

{flags}

仍未完成：整段日期的exact-scope嵌套资格与完整父概率替换；融合权重、中心/宽度参数及否决规则的完整因果重拟；将新父概率接回整段P0-M/P1后重新评分。当前嵌套探针只覆盖每站一个10-05外层时点，不能称作全日期OOS。

## 哪里取得可比较的低层云

- 实况：已真实收到的官方AWC METAR/SPECI原文云层，包含覆盖码和云底。美国高频ASOS来源另按精确来源资格仲裁，未获资格时使用合法AWC后备。
- 预报：有真实历史回执、且有效时段覆盖目标小时的官方TAF原文，分别取最新及提前至少24小时已收到的版本。
- Open-Meteo的`cloud_cover_low`是模式预报；ERA5是事后再分析。它们均不能冒充实时低云实测。亚洲显式JAXA卫星辐射产品仍是候选，历史到达与QA资格未证实。

本研究的“低层覆盖”是指定高度以下报告云层的最大覆盖区间，不是NWP低云百分比或多层并集。SCT=[3/8,4/8]、BKN=[5/8,7/8]，只有区间不重叠才认定确定覆盖差；TEMPO/PROB保留可能场景，不猜概率。CAVOK/NSC/NCD在6500ft不能补零。高度采用严格`base < threshold`。

云底只取各预报场景唯一同覆盖码/云型匹配。**云底下降不能直接等同于遮阳增强**：原文没有光学厚度、云顶或实测辐射。辐射较预报低也不能唯一反推出低云量变大。

## 独立增量项与限定样本结果

覆盖项为`exp(-β × 覆盖增量 × 相对已报最高桶中心的再升幅°C)`，β∈[0,4]；更多云只能压低再升，更少云只能抬高。云底项单独拟合，β∈[-4,4]，不预设方向。两项均未混入现有弱特征。

统一顺序：未加floor的direct posterior → 独立增量似然 → 同一service floor一次。β=0、未知或零增量逐行精确恒等；硬排除桶保持零。修复了前一版candidate重复加floor的评分错误。另核验独立云报文的来源记录：8,018条研究锚点中245条没有截止前唯一可见的官方origin，将其云差设为未知再重跑；以下只引用这版来源门+单floor结果。16日P1基准参数、36日TAF基准及固定强度测试均与修复前精确一致，变化只来自增量训练证据的来源门。

测试窗09-21至10-06，共4,624锚点、494站日、16个外推日。每个测试日仅用此前日期、且真值当时已知的数据训练。先站日等权，再按日期块形成区间；CRPS/误差以°C计。差值=候选−基准，负值较好。

|独立项与群体|测试行|站日|Δ负LogScore [95%CI]|ΔBrier [95%CI]|
|---|---:|---:|---|---|
{chr(10).join(rows)}

最新覆盖项拟合β约{min(coverage_betas['latest']):.3f}–{max(coverage_betas['latest']):.3f}；提前24小时约{min(coverage_betas['day1']):.3f}–{max(coverage_betas['day1']):.3f}。最新全部样本的小收益在2/4日连续块bootstrap及前后半段仍保持，但可比证据子群的LogScore区间跨零。不能据此宣布全链或强似然通过。

固定强度β=1/2/4是预先列出的敏感性实验。最新β=1全部样本ΔLogScore约−0.00315但区间跨零；提前24小时β=1的可比证据组约+0.00767且区间跨零，最高温均值MAE显著变差。**没有证据支持“越强一定越差”，也没有强似然晋级证据。** 晴于预报的子群在拟合强度下最高温均值MAE变差，需在完整链上复核。

## 成都10月7日25°C案例

12–14时实况SCT030，对比已收到的SCT050：6500ft覆盖差=0，云底约降低610m；15时BKN030，对比SCT050，覆盖至少增加1/8天空；16时回SCT030。5000ft阈值会排除SCT050，产生阈值敏感的“低层更多云”。不能把这两类变化混为一谈。

10-05以前训练出的最新6500ft覆盖β={fifteen['beta']:.5f}：12–14时该项恒等；15时使每再升1°C的相对odds乘约{fifteen['one_c_rise_odds_multiplier']:.4f}。云底β约{noon['beta']:+.5f}，降610m反而使相对odds乘约{noon['one_c_rise_odds_multiplier']:.4f}，说明该代理的遮阳方向未证实。该案例5份云报文的官方来源均已核验在截止前可见。

这只是独立项的odds数学诊断，**不是完整P(25)复现**，也不能证否复现可能性。最新TAF TX28属7日；提前24小时能配上的TX25属6日，不得错用。提前24小时云段12时收到略迟，13时有配，14时旧报已过期。

## 新推进的父链工作

生产builder已在独立研究进程中使用真实原生成员`archive_query_at_utc`、内层当时已知真值修订和实际提交的市场桶合同。决策时钟是输入实际到达与桶合同实际可见时点的最大值，明确为重建时钟。后到闭日的原始成员仅可train-only；无真实桶合同不能作holdout，但不抹掉可训练的温度事实。

隔离编译使用SQLite内存库，SSOT以`mode=ro`附加；只在内存写研究证书/模拟激活。对source的增删改及结构写入已做拒绝测试。证书通过只代表该组件可编译，不代表当年已有这些激活，也不代表完整P1已通过。

融合重拟输入现已重建{facts['counts']['representative_facts']:,}条代表事实、{facts['counts']['truth_revisions']:,}份真值修订。使用现行单系统概率、真实C/F市场桶边界与原始父提交时钟，不沿用整数°C或模型化到达的旧回放。按UTC午夜固定研究时刻拟合当前融合公式，逐折保存真实可见真值与完整训练引用；它仍使用原来的seating资格，且尚无新父链的连续served账本来重建veto，所以明确为研究参数，不能单独授予全链资格。

本地精确输入包检索：{missing['counts']['date_directories']}个日期目录、{missing['counts']['filenames_scanned']:,}文件，{missing['counts']['EXACT_SHA_NOT_IN_LOCAL_ARCHIVE']}个缺包均未找到同SHA副本。没有替换或伪造父输入；其他同站日候选已检索：{alternates['counts']['packet_objects_scanned']:,}个对象中，6个SHA/站日不一致的对象被拒绝；272锚点均找到提前收到且几何一致的候选，并通过至少两独立家族的历史成员精确来源绑定。它们仍需自己的嵌套资格与完整下游重放，未替换或激活任何父概率。

ASOS审计发现旧artifact声明MADIS，而匹配事实属于IEM，不能改名授予资格。4747个美国锚点中MADIS有490个覆盖合格，但现有artifact均不满足精确来源绑定，故唯一赢家是AWC；这不是永久无数据，也不是必须阻断AWC后备的额外门。

## 检验、复跑与下一个主线

原文14组边界、23/25小时DST及29,602对全量核验已通过；数值7组/1000随机支持集、来源5组、隔离2组、融合时钟/真值/F桶3组、存储2组，共33组边界检查通过；相关生产pytest此前26 passed。组件全量验证记录在`current_verification.json`和`nested_verification.json`，其中后者必须48站全部COMPONENT_AUDIT_PASS才算阶段闭环。

入口：`nested_batch.py --summarize-only`汇总当前版本；`verify_nested.py`核验每个引用对象；`compiler_batch.py`只在内存编译；`fusion_facts.py`/`fusion_refit.py`重建融合输入/参数；`compiler_batch.py --fusion-refit`接入研究融合参数；`report.py`刷新报告与Sources receipt。全程资源有界、SSOT只读，未重启服务或修改生产配置。

**下一个主线任务：补齐中心/宽度及完整融合父链，完成全日期现行P0→P0-M→P1严格重放，再复验独立覆盖/云底增量；未过完整门前不改生产。**
'''
    (HERE/'RESULTS.md').write_text(text)
    names=['results.json','verification.json','current_verification.json','restricted_refitted_fit.json','score_sensitivity.json','case_increment_math.json','nested_batch.json','nested_verification.json','compiler_batch.json','compiler_batch.fusion.json','fusion_facts.json','fusion_refit.json','missing_packets.json','alternative_packets.json','alternative_member_verification.json','cloud_origin_audit.json','cloud_origin_rescore_validation.json','cleanup.json']
    fingerprint=dict(schema='research_current_evidence_fingerprints_v2',production_modified=False,claim_scope='limited component and restricted population evidence; no full-chain promotion',sources=[dict(path=name,sha256=old.sha_file(HERE/name)) for name in names if (HERE/name).exists()]);(HERE/'sources.json').write_text(json.dumps(fingerprint,ensure_ascii=False,indent=2))
    latest=fit['variants']['latest_6500_coverage_logscore'];day1=fit['variants']['day1_6500_coverage_logscore']
    receipt=dict(schemaVersion=1,items=[dict(id='restricted-cloud-increment',title='云覆盖差的限定样本结果',queries=[dict(id='restricted-walk-forward',source=dict(label='正式观测前缀研究',files=['restricted_refitted_fit.json','strict_fit.py','current_verification.json','score_sensitivity.json','cloud_origin_audit.json','cloud_origin_rescore_validation.json'],filters=['先站日等权，再按日期块统计','每个外推日只用此前且真值当时已知的数据训练','覆盖差阈值6500ft','245条缺唯一可见官方origin的云证据已设为未知并重跑'],metricDefinitions=['负LogScore和Brier差值为云增量候选减相同基准，负值表示改善。'],caveats=['这些结果仅覆盖输入获准子群，父链资格和融合/中心宽度重拟尚未完整，不是全48站晋级证据。']),reportingPeriod='2026-09-21至2026-10-06：16个外推日',rows=[dict(group='最新覆盖：全部',rows=latest['all']['rows'],station_days=latest['all']['station_days'],delta_logscore=latest['all']['mean'][0],ci=latest['all']['ci95'][0]),dict(group='最新覆盖：可比证据',rows=latest['known_only']['rows'],station_days=latest['known_only']['station_days'],delta_logscore=latest['known_only']['mean'][0],ci=latest['known_only']['ci95'][0]),dict(group='提前24h覆盖：全部',rows=day1['all']['rows'],station_days=day1['all']['station_days'],delta_logscore=day1['all']['mean'][0],ci=day1['all']['ci95'][0])],columns=['group','rows','station_days','delta_logscore','ci'])]),dict(id='chengdu-cloud-change',title='成都10月7日的覆盖与云底变化',queries=[dict(id='raw-cloud-and-odds',source=dict(label='真实回执绑定TAF与官方报文',files=['results.json','verification.json','case_increment_math.json','case_increment_math.py'],metricDefinitions=['6500ft覆盖增量比较报文云层最大覆盖区间；云底差只比较唯一同覆盖码/云型匹配。'],filters=['ZUUU：2026-10-07当地12–16时'],caveats=['云底下降不能直接等同于遮阳增强。','odds乘数仅是独立项数学诊断，未复现完整P(25)。']),summary='12–14时覆盖差为零、云底降低约610m；15时BKN相对SCT至少增加1/8天空。')]),dict(id='replay-and-missing-packets',title='父链重建与缺失输入',queries=[dict(id='parent-input-search',source=dict(label='本地原生包归档与隔离研究',files=['missing_packets.json','nested_batch.json','compiler_batch.json','fusion_facts.json','fusion_refit.json'],caveats=['每站10-05嵌套探针和研究融合重拟不等于全日期完整链检验。','所有生产概率、SSOT和服务保持未修改。']),rows=[dict(metric='已扫描日期目录',value=missing['counts']['date_directories']),dict(metric='已扫描文件',value=missing['counts']['filenames_scanned']),dict(metric='未找到同SHA的原始父包',value=missing['counts']['EXACT_SHA_NOT_IN_LOCAL_ARCHIVE']),dict(metric='重建融合代表事实',value=facts['counts']['representative_facts'])],columns=['metric','value'])])])
    for item in receipt['items']:
        for query in item['queries']:
            if 'ci' in query.get('columns',[]):
                query['columns']=[c for c in query['columns'] if c!='ci']+['ci95_low','ci95_high']
                for row in query['rows']:row['ci95_low'],row['ci95_high']=row.pop('ci')
    (HERE/'sources_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
