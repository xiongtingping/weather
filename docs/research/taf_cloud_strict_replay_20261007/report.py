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
    ledger=load('fusion_served_ledger.json');ledger_audit=load('fusion_ledger_verification.json');comparison=load('fusion_compiler_comparison.json');compaction=load('nested_compaction.json');inventory=load('nested_multiday_inventory.json');multi=load('nested_verification.multiday.json');multi_compile=load('compiler_multiday.json');packet_clocks=load('original_packet_clock_verification.json')
    additions=''
    served=load_fit_report(HERE/'restricted_served_parent_fit.json') if (HERE/'restricted_served_parent_fit.json').exists() else None
    parent_section=''
    if served:
        prow=[]
        for title,key,group in [('最新覆盖差：全部','latest_6500_coverage_logscore','all'),('最新覆盖差：可比云证据','latest_6500_coverage_logscore','known_only'),('最新覆盖差：12–18时','latest_6500_coverage_logscore','afternoon'),('提前≥24h覆盖差：全部','day1_6500_coverage_logscore','all'),('最新云底差：12–18时','latest_6500_base_logscore','afternoon'),('最新覆盖β=1：全部','latest_6500_coverage_fixed_1','all')]:
            x=fit['variants'][key][group];y=served['variants'][key][group]
            prow.append(f"|{title}|{metric(x)}|{metric(y)}|{metric(x,1)}|{metric(y,1)}|")
        sb=[next(x['beta'] for x in r['increments'] if x['variant']=='latest_6500_coverage_logscore') for r in served['p1_fit_receipts']]
        parent_section=f'''
## 父链敏感性：把P0父概率换成当时实际服务的P0（2026-10-08）

同一批8,018锚点、同一云差、同一P0-M/P1逐日因果重拟，只把P0父换成生产在该时刻实际已服务的最新P0（22个历史版本，与研究重拟父的概率差平均TV 0.106、中心差平均|0.195|°C）。若增量结论对父链不敏感，补全父链就不会改判；反之补全父链才有意义。

|独立项与群体|ΔLogScore 研究父|ΔLogScore 服务父|ΔBrier 研究父|ΔBrier 服务父|
|---|---|---|---|---|
{chr(10).join(prow)}

服务父下最新覆盖β约{min(sb):.3f}–{max(sb):.3f}，与研究父同量级。**结论：最新覆盖差的“两分数同时显著”只在研究父上成立，换成服务父后Brier区间跨零、LogScore上界贴零；提前24小时覆盖则反过来只在服务父上LogScore显著、Brier跨零。两种父下效应都在约0.001 nat量级，符号稳定但显著性随父链翻转——增量本身弱到父链差异就能改判。** 云底差两种父下12–18时都显著变差，确认不用。
'''
    if ledger and ledger_audit and comparison:
        additions+=f"\n新增融合账本：{ledger['rows']:,}条代表决策、{ledger_audit['counts']['veto_cutoffs']}个每日截止时点、{ledger_audit['counts']['veto_training_references']:,}条因果引用全量核验通过；这份限定账本上未触发否决。接入后{comparison['exact_probability_stations']}站概率及权重与仅重拟版逐项精确相等，{comparison['exact_refusal_stations']}站仍因原始精确资格不足拒绝。它仍使用原来的座位资格，不是完整新父链的连续服务账本。\n"
    if compaction:
        additions+=f"\n资源闭环：{sum(compaction['counts'].values()):,}个嵌套对象的展开内容哈希全部保持一致；仅去除重复系统评估的序列化，存储从{compaction['physical_before']/1024**2:.1f}MiB降到{compaction['physical_after']/1024**2:.1f}MiB，2MiB单对象及128MiB阶段上限保持。\n"
    if inventory:
        additions+=f"\n多日期盘点恢复了{inventory['census'].get('original_parent_clock_restored_independent_of_old_replay_gate',0)}个旧研究行缺失的父包时钟：它们的原始提交回执实际仍在，不能因旧重放被拒就抹掉。可调度的原始站日为{inventory['census']['scheduled_station_dates']:,}，盘点时已有48份单日期报告；其余仍待展开。缺失的272个原始包仍独立登记，恢复时钟没有恢复文件。\n"
    if multi:
        additions+=f"\n日期边界验收：成都、美国、欧洲各取9月1日、9月21日、10月6日，{multi['counts'].get('COMPONENT_AUDIT_PASS',0)}/9份新增报告通过原始父时钟、内层真值修订、参数、真实桶合同和所有引用对象核验。该9份边界覆盖不代表全日期资格已完成。隔离编译：{multi_compile['counts'] if multi_compile else '尚未结束'}。\n"
    if packet_clocks:
        additions+=f"\n回绕核验：{packet_clocks['counts']['passed']}份已建报告的原始native包均通过内容哈希、站点、事件日及实际到达≤父决策提交时钟的全量补验；编译入口增加同一检查，未知/迟到包立即拒绝。\n"
    text=f'''# 实况减预报云增量：严格重放研究（2026-10-08）

熊先生，**覆盖差有小幅改善信号，强似然尚未获准；云底差的物理方向没有证实。完整现行链检验仍未完成，生产没有修改。** 本报告替代此前“尚未计算任何概率成绩”的阶段记录。

## 可验证终点与阶段复盘

终点：全48站按真实输入到达及输出提交时钟重建现行P0/P0-M/P1；参数只用当时已知的闭日真值；云覆盖差和云底差分别作独立增量似然，完成滚动样本外评分和单位、来源、支持集、强度边界检验；达标才接生产。

已完成：29,602对原文/实况重建；48站原生上游审计；11,156个P0锚点的当前单系统参数重拟（{p0['counts']['ASOF_INPUT_ARITHMETIC_NOT_CURRENT_CHAIN_OOS']:,}成功）；{prefix['counts']['OFFLINE_PREFIX_REBUILT_NOT_PROMOTED']:,}条正式前缀研究锚点；P0-M和P1逐日因果拟合；16日限定样本评分。已恢复区域特征真实fetch时钟、辐射历史version视图和真值修订时钟。

{flags}
{additions}

仍未完成：整段日期的exact-scope嵌套资格与完整父概率替换；中心/宽度参数及完整新父链下融合权重、否决规则的因果重拟；将新父概率接回整段P0-M/P1后重新评分。全站探针目前仍只覆盖每站一个10-05外层时点，另有日期边界验收，不能称作全日期OOS。

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

{parent_section}
## 成都10月7日25°C案例

12–14时实况SCT030，对比已收到的SCT050：6500ft覆盖差=0，云底约降低610m；15时BKN030，对比SCT050，覆盖至少增加1/8天空；16时回SCT030。5000ft阈值会排除SCT050，产生阈值敏感的“低层更多云”。不能把这两类变化混为一谈。

10-05以前训练出的最新6500ft覆盖β={fifteen['beta']:.5f}：12–14时该项恒等；15时使每再升1°C的相对odds乘约{fifteen['one_c_rise_odds_multiplier']:.4f}。云底β约{noon['beta']:+.5f}，降610m反而使相对odds乘约{noon['one_c_rise_odds_multiplier']:.4f}，说明该代理的遮阳方向未证实。该案例5份云报文的官方来源均已核验在截止前可见。

这只是独立项的odds数学诊断，**不是完整P(25)复现**，也不能证否复现可能性。最新TAF TX28属7日；提前24小时能配上的TX25属6日，不得错用。提前24小时云段12时收到略迟，13时有配，14时旧报已过期。

## 新推进的父链工作

生产builder已在独立研究进程中使用真实原生成员`archive_query_at_utc`、内层当时已知真值修订和实际提交的市场桶合同。决策时钟是输入实际到达与桶合同实际可见时点的最大值，明确为重建时钟。后到闭日的原始成员仅可train-only；无真实桶合同不能作holdout，但不抹掉可训练的温度事实。

隔离编译使用SQLite内存库，SSOT以`mode=ro`附加；只在内存写研究证书/模拟激活。对source的增删改及结构写入已做拒绝测试。证书通过只代表该组件可编译，不代表当年已有这些激活，也不代表完整P1已通过。

融合重拟输入现已重建{facts['counts']['representative_facts']:,}条代表事实、{facts['counts']['truth_revisions']:,}份真值修订。使用现行单系统概率、真实C/F市场桶边界与原始父提交时钟，不沿用整数°C或模型化到达的旧回放。按UTC午夜固定研究时刻拟合当前融合公式，逐折保存真实可见真值与完整训练引用；其代表决策的连续服务权重和否决证据已重建并核验，但仍使用原来的seating资格，缺完整新父链的全部根决策账本，不能单独授予全链资格。

本地精确输入包检索：{missing['counts']['date_directories']}个日期目录、{missing['counts']['filenames_scanned']:,}文件，{missing['counts']['EXACT_SHA_NOT_IN_LOCAL_ARCHIVE']}个缺包均未找到同SHA副本。没有替换或伪造父输入；其他同站日候选已检索：{alternates['counts']['packet_objects_scanned']:,}个对象中，6个SHA/站日不一致的对象被拒绝；272锚点均找到提前收到且几何一致的候选，并通过至少两独立家族的历史成员精确来源绑定。它们仍需自己的嵌套资格与完整下游重放，未替换或激活任何父概率。

ASOS审计发现旧artifact声明MADIS，而匹配事实属于IEM，不能改名授予资格。4747个美国锚点中MADIS有490个覆盖合格，但现有artifact均不满足精确来源绑定，故唯一赢家是AWC；这不是永久无数据，也不是必须阻断AWC后备的额外门。

## 检验、复跑与下一个主线

原文14组边界、23/25小时DST及29,602对全量核验已通过；研究测试现共50组通过（原33组，加融合账本5组、无损存储5组、多日期调度2组、原始父时钟及包到达5组）；相关生产pytest此前26 passed。组件全量验证记录在`current_verification.json`和`nested_verification.json`，其中后者必须48站全部COMPONENT_AUDIT_PASS才算阶段闭环。

入口：`nested_batch.py --summarize-only`汇总当前版本；`verify_nested.py`核验每个引用对象，`--stations/--days`验指定日期；`fusion_ledger.py`与`verify_fusion_ledger.py`构建、核验代表服务账本；`compiler_batch.py --fusion-ledger`只在内存接入该账本；`nested_multiday.py --inventory-only`盘点实际父时钟；`compact_nested.py`去重存储并核验原始哈希；`report.py`刷新报告与Sources receipt。全程资源有界、SSOT只读，未重启服务或修改生产配置。

**下一个主线（2026-10-08 复盘后改）：云覆盖/云底增量作P1似然建议结案，生产不改。** 两种父链下覆盖增量都只有约0.001 nat、显著性随父链翻转，云底增量12–18时两种父下都显著变差。补全父链还剩1,578站日组件（KLGA 09-25实测28.5秒/站日、234MB，合计约15小时整机负载），之后还要做中心/宽度与融合的因果重拟及全段P0-M/P1重放；这些只为裁决一个约0.001 nat的项。要重开须换信息源（有历史的实测低云/日照），不是补父链。
'''
    (HERE/'RESULTS.md').write_text(text)
    names=['results.json','verification.json','current_verification.json','restricted_refitted_fit.json','score_sensitivity.json','case_increment_math.json','nested_batch.json','nested_verification.json','compiler_batch.json','compiler_batch.fusion.json','fusion_facts.json','fusion_refit.json','missing_packets.json','alternative_packets.json','alternative_member_verification.json','cloud_origin_audit.json','cloud_origin_rescore_validation.json','cleanup.json','fusion_served_ledger.json','fusion_ledger_verification.json','compiler_batch.ledger.json','fusion_compiler_comparison.json','nested_compaction.json','nested_multiday_inventory.json','nested_verification.multiday.json','compiler_multiday.json','original_packet_clock_verification.json','restricted_served_parent_fit.json']
    fingerprint=dict(schema='research_current_evidence_fingerprints_v2',production_modified=False,claim_scope='limited component and restricted population evidence; no full-chain promotion',sources=[dict(path=name,sha256=old.sha_file(HERE/name)) for name in names if (HERE/name).exists()]);(HERE/'sources.json').write_text(json.dumps(fingerprint,ensure_ascii=False,indent=2))
    latest=fit['variants']['latest_6500_coverage_logscore'];day1=fit['variants']['day1_6500_coverage_logscore']
    receipt=dict(schemaVersion=1,items=[dict(id='restricted-cloud-increment',title='云覆盖差的限定样本结果',queries=[dict(id='restricted-walk-forward',source=dict(label='正式观测前缀研究',files=['restricted_refitted_fit.json','strict_fit.py','current_verification.json','score_sensitivity.json','cloud_origin_audit.json','cloud_origin_rescore_validation.json'],filters=['先站日等权，再按日期块统计','每个外推日只用此前且真值当时已知的数据训练','覆盖差阈值6500ft','245条缺唯一可见官方origin的云证据已设为未知并重跑'],metricDefinitions=['负LogScore和Brier差值为云增量候选减相同基准，负值表示改善。'],caveats=['这些结果仅覆盖输入获准子群，父链资格和融合/中心宽度重拟尚未完整，不是全48站晋级证据。']),reportingPeriod='2026-09-21至2026-10-06：16个外推日',rows=[dict(group='最新覆盖：全部',rows=latest['all']['rows'],station_days=latest['all']['station_days'],delta_logscore=latest['all']['mean'][0],ci=latest['all']['ci95'][0]),dict(group='最新覆盖：可比证据',rows=latest['known_only']['rows'],station_days=latest['known_only']['station_days'],delta_logscore=latest['known_only']['mean'][0],ci=latest['known_only']['ci95'][0]),dict(group='提前24h覆盖：全部',rows=day1['all']['rows'],station_days=day1['all']['station_days'],delta_logscore=day1['all']['mean'][0],ci=day1['all']['ci95'][0])],columns=['group','rows','station_days','delta_logscore','ci'])]),dict(id='chengdu-cloud-change',title='成都10月7日的覆盖与云底变化',queries=[dict(id='raw-cloud-and-odds',source=dict(label='真实回执绑定TAF与官方报文',files=['results.json','verification.json','case_increment_math.json','case_increment_math.py'],metricDefinitions=['6500ft覆盖增量比较报文云层最大覆盖区间；云底差只比较唯一同覆盖码/云型匹配。'],filters=['ZUUU：2026-10-07当地12–16时'],caveats=['云底下降不能直接等同于遮阳增强。','odds乘数仅是独立项数学诊断，未复现完整P(25)。']),summary='12–14时覆盖差为零、云底降低约610m；15时BKN相对SCT至少增加1/8天空。')]),dict(id='replay-and-missing-packets',title='父链重建与缺失输入',queries=[dict(id='parent-input-search',source=dict(label='本地原生包归档与隔离研究',files=['missing_packets.json','nested_batch.json','compiler_batch.json','fusion_facts.json','fusion_refit.json'],caveats=['每站10-05嵌套探针和研究融合重拟不等于全日期完整链检验。','所有生产概率、SSOT和服务保持未修改。']),rows=[dict(metric='已扫描日期目录',value=missing['counts']['date_directories']),dict(metric='已扫描文件',value=missing['counts']['filenames_scanned']),dict(metric='未找到同SHA的原始父包',value=missing['counts']['EXACT_SHA_NOT_IN_LOCAL_ARCHIVE']),dict(metric='重建融合代表事实',value=facts['counts']['representative_facts'])],columns=['metric','value'])])])
    for item in receipt['items']:
        for query in item['queries']:
            if 'ci' in query.get('columns',[]):
                query['columns']=[c for c in query['columns'] if c!='ci']+['ci95_low','ci95_high']
                for row in query['rows']:row['ci95_low'],row['ci95_high']=row.pop('ci')
    if ledger and ledger_audit and comparison:
        receipt['items'].append(dict(id='causal-fusion-closure',title='融合账本与资源阶段核验',queries=[dict(id='fusion-serving-and-lossless-storage',source=dict(label='隔离研究组件核验',files=['fusion_served_ledger.json','fusion_ledger_verification.json','fusion_compiler_comparison.json','nested_compaction.json','nested_multiday_inventory.json','nested_verification.multiday.json','compiler_multiday.json'],caveats=['原座位资格仍保留；没有完成整段现行父链，不能晋级生产。','恢复677个原始父时钟不等于恢复272个缺失原始包。']),rows=[dict(metric='因果服务代表决策',value=ledger['rows']),dict(metric='每日否决截止时点',value=ledger_audit['counts']['veto_cutoffs']),dict(metric='因果引用核验',value=ledger_audit['counts']['veto_training_references']),dict(metric='接入后概率精确相等站',value=comparison['exact_probability_stations']),dict(metric='无损去重节省MiB',value=round(compaction['physical_bytes_saved']/1024**2,1) if compaction else 0)],columns=['metric','value'])]))
    if packet_clocks and ledger and ledger_audit and comparison:
        query=receipt['items'][-1]['queries'][0]
        query['source']['files'].append('original_packet_clock_verification.json')
        query['rows'].append(dict(metric='原始包身份及到达时钟核验',value=packet_clocks['counts']['passed']))
    (HERE/'sources_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
