"""Small reproducible research readout and source receipt; no production writes."""
import json
from pathlib import Path
from audit import HERE, guard


def main():
    guard();r=json.loads((HERE/'results.json').read_text());v=json.loads((HERE/'verification.json').read_text())
    table=[]
    for mode,label in [('latest','当时最新且已收到'),('day1','实况时刻至少提前24小时')]:
        s=r['summaries'][mode]
        table.append(f"| {label} | {s['matched']:,}/{s['rows']:,} ({s['matched']/s['rows']:.1%}) | {s['stations']} | {s['usable_nonstale_by_threshold']['6500']:,} |")
    zu={m:r['summaries'][m]['by_station']['ZUUU'] for m in ('latest','day1')}
    current={m:r['summaries'][m]['by_version']['p1_joint_likelihood_v38'] for m in ('latest','day1')}
    case=[]
    for x in r['case']:
        if not 12<=x['local_hour']<=16:continue
        actual=' / '.join(str(l['cover'])+(' '+str(l['base'])+'ft' if l['base'] is not None else '') for l in x['obs_layers'])
        latest=x['latest']
        fc=' / '.join(str(l['cover'])+(' '+str(l['base'])+'ft' if l.get('base') is not None else '') for l in latest['scenarios'][0]) if latest and latest['scenarios'] else '未知'
        case.append(f"| {x['local_hour']}:00 | {actual} | {fc} | {'覆盖量更高' if latest and latest['directions']['6500']=='more' else '覆盖类别相同，云底更低'} | {'有' if x['day1'] else '无'} |")
    p=r['profile']
    text=f'''# TAF × METAR 直接云量差分核查（2026-10-07）

终点线：确认近36天及成都案例能否按真实历史回执、有效时段、云底高度配对；量化覆盖和缺口，判断下一步可否进行独立增量检验。本轮完成该终点线；没有拟合概率权重、没有修改生产。

结论：直接云层对比路径已打通，但航空云层不是完整天空低云百分比。需要把云覆盖类别增量与云底降低分开；本轮不能证明独立强似然有效，也不能据此上线。

## 覆盖

输入复用已审计的09-01至10-06、48站、19,618个当地小时P1样本；每站每天每个当地小时最后一份。旧服务记录跨版本，全部维持 `observational_candidate_not_oos`。

| 预报选择方式 | 有回执且覆盖实况时刻的TAF | 站数 | 6500ft以内云量可比较且实况未超过2小时 |
|---|---:|---:|---:|
{chr(10).join(table)}

配对率的分母是所有19,618行，不能把“有预报”误报为“云量可比较”。6500ft阈值下，CAVOK/NSC/NCD只约束重要低层云，不约束整个6500ft以下天空，故列为未知；其余缺云底、垂直能见度/天空遮蔽亦不补零。

成都：最新预报配对 {zu['latest']['matched']}/{zu['latest']['rows']}；至少提前24小时配对 {zu['day1']['matched']}/{zu['day1']['rows']}。TAF常见有效期24或30小时，发布时间和实际到达延迟进一步缩窄至少24小时提前量的重叠窗口；不与“前一日发布”混称。

## 成都10月7日（当地时间 Asia/Shanghai）

| 实况小时 | METAR实况 | 当时已收到、且有效的最新TAF | 两层均纳入6500ft以下后的对比 | 至少提前24h且有效的TAF |
|---|---|---|---|---|
{chr(10).join(case)}

12–13时选择10-06 21:02Z发布、当天00–24Z有效的TAF；14–16时选择10-07 03:01Z发布、06Z起有效的TAF。没有拿当天09:05Z发布、12Z起有效的后来预报解释之前午后。

SCT是3–4/8，BKN是5–7/8；030为3000ft≈914m，050为5000ft≈1524m。12–14时实况SCT030对预报SCT050主要体现云底降低；15时BKN030对SCT050才有至少1/8天空的确定覆盖量增加。13时达到25°C，15时覆盖量增多这一事实不排除之前存在云的辐射影响，也不证明因果。

把阈值设为云底严格低于5000ft时，预报SCT050被分到阈值外，SCT030在阈值内，会把12–14时也标为“更多低层云”；扩到6500ft、纳入两层后该覆盖增量消失。这是高度分箱敏感性，不能无条件当作强遮阳证据。云底降低与云顶低、云厚、透光率是不同物理量。

12时至少24h配对缺失，因为上一日03:02Z发行的TAF实际收到略晚于04Z截止；13时该份仍有效且已满足24h；14时其06Z有效期已结束。该份TX25有效时刻在10-06，不能用于10-07最高温，只使用覆盖10-07相应时刻的云层段。

## 来源与时序

- 原文和解码来自本地 `data/runtime/taf_raw_archive/`，逐行验证站号、发行时间、原文哈希。归档写入器明示外侧空白trim；{p['canonical_outer_whitespace_rows']}行按该既有canonical口径核对，不另猜测字段。
- 共扫描 {p['raw_rows']:,}条，{p['backfill_rows']:,}条是后来回补。回补自带的合成收到时间从不证明当时到达。
- SSOT `calibration_forward_taf_capture` 按站号和 `source_object_sha256` 精确绑定旧本地回执，同时核对发行及整个有效期。有效期不一致的{p['receipt_validity_mismatch']}条不采用。回补的原文与旧回执可绑定，但回补解码的历史到达/QA不可由原文哈希证明。
- NOAA TGFTP同一原文没有解码云层；{p['missing_decoded_forecast_rows']:,}条缺解码不当作“与AWC冲突”。{p['decode_conflict_bulletins']}份真正云层/时段解码冲突，以及{p['corrected_bulletins_excluded']:,}份COR予以排除。
- METAR来自 `archive.db.reported_trajectory_observations`：站号和observaton_id精确绑定，AWC来源，valid/first_seen/committed均不晚于P1决策，排除疑似更正；趋势和RMK不当作当前云量。按最新绑定核对各站时区。
- TAF在实况valid时刻已发布且已收到，才作为基准；有效期半开区间。BECMG过渡保留新旧状态，TEMPO/PROB保留可能场景，未明示变化的元素按规则继承。FM完整组缺云层则未知；从不把PROB概率加权成精确云百分比。
- 量是“指定云底阈值下、已报告各层中最大的覆盖区间”，不是多层并集面积、NWP low_cloud_cover或卫星云顶分类。

## 验证与剩余阻断

{v['boundary_test_groups']}组边界测试通过；逐行检查全部{v['full_data']['rows']:,}行、{v['full_data']['matched_pairs']:,}对有效配对的双时钟截止、半开有效期、COR排除及四档阈值下方向。已折返核查来源同原文、缺解码和真正冲突的区别。

现行 `p1_joint_likelihood_v38` 仍只有10-04至10-06三个闭日、1,471行；其中最新TAF可配{current['latest']['matched']}行，至少提前24h可配{current['day1']['matched']}行。旧链及回补解码不能直接充当现行链36天严格OOS。不存在本轮“云增量提高预测准确率”的评分结果。

下一个主线任务：对有原文哈希与历史真实回执的闭日，用确定性原文解码重建云层，补现行P1链strict-as-of历史重放；云覆盖差、云底差分列，只在足够严格OOS样本上拟合独立物理似然，过门后才考虑生产。

## 复现

`python3 docs/research/taf_cloud_increment_20261007/audit.py`

`python3 docs/research/taf_cloud_increment_20261007/verify.py`

`python3 docs/research/taf_cloud_increment_20261007/report.py`

主要结果 `results.json`，全量配对 `paired_rows.jsonl.gz`（本地研究输入，不入git），边界/全量验收 `verification.json`。原始文件按读取时字节长度冻结并有SHA256；数据库只读不复制生产数据库。RSS1GiB、行2MiB、各阶段输入/输出128MiB、剩余磁盘2GiB，SQL每次60秒上限，超限fail closed。
'''
    (HERE/'RESULTS.md').write_text(text)
    code="import json\nfrom pathlib import Path\nbase=Path.cwd()\nif not (base/'results.json').exists(): base=base/'docs/research/taf_cloud_increment_20261007'\nr=json.loads((base/'results.json').read_text())\nfor mode,s in r['summaries'].items():\n    print(mode, s['matched'], '/', s['rows'], 'usable6500=', s['usable_nonstale_by_threshold']['6500'])\nprint('ZUUU', {m:r['summaries'][m]['by_station']['ZUUU'] for m in ('latest','day1')})\n"
    nb=dict(cells=[dict(cell_type='markdown',metadata={},source=['# TAF/METAR配对审计\n','只读研究。原始SQL与指纹在results.json，完整时序及边界检验在audit.py/verify.py；没有严格OOS或生产晋级。']),dict(cell_type='code',metadata={},source=code.splitlines(keepends=True),execution_count=None,outputs=[])],metadata={'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},nbformat=4,nbformat_minor=5)
    (HERE/'analysis.ipynb').write_text(json.dumps(nb,ensure_ascii=False,indent=2))
    sources={'schemaVersion':1,'items':[
        {'id':'coverage','title':'历史TAF与METAR的配对覆盖','queries':[{'id':'paired-summary','source':{'label':'本地TAF与METAR审计','files':[{'label':'results.json'},{'label':'audit.py'},{'label':'verification.json'}],'tables':[{'name':'calibration_forward_taf_capture'},{'name':'reported_trajectory_observations'}],'metricDefinitions':[{'label':'配对率','definition':'有原文哈希对应的本地历史回执、且有效期覆盖实况的TAF行数，占19,618个已审计小时样本的比例。'}],'filters':['实况来源：AWC','排除疑似更正实况及COR预报','预报发行和到达均早于实况时刻'],'caveats':['旧P1版本混合，现行P1仅有三个闭日；配对结果全部是研究候选，不是严格OOS。','回补原文哈希可以证明旧回执的身份，不能证明回补解码的历史可用性。'],'evidenceFlow':[{'kind':'validation','title':'时序与边界','detail':f"12组边界测试通过；全量{v['full_data']['matched_pairs']}对配对检查双时钟截止、有效期与云量区间方向。"}]},'reportingPeriod':'2026-09-01至2026-10-06，48站','columns':['mode','matched','total','usable_6500_fresh'],'rows':[{'mode':mode,'matched':s['matched'],'total':s['rows'],'usable_6500_fresh':s['usable_nonstale_by_threshold']['6500']} for mode,s in r['summaries'].items()]}]},
        {'id':'zuuu','title':'成都云量增多与云底降低是两种变化','queries':[{'id':'case-clouds','source':{'label':'ZUUU官方报文历史','files':[{'label':'results.json'}],'metricDefinitions':[{'label':'云层覆盖类别','definition':'SCT表示3–4/8天空，BKN表示5–7/8；云底单位为英尺离地高度。'}],'filters':['按各实况时刻已收到且有效的最新TAF比较'],'caveats':['6500ft以下两层均纳入时，12–14时覆盖类别相同；15时实况覆盖类别高于预报。','云底降低不能直接推出更强遮阳，今天的案例也不是闭日OOS。']},'reportingPeriod':'2026-10-07成都当地12–16时','columns':['local_hour','observed','forecast'],'rows':[{'local_hour':x['local_hour'],'observed':x['obs_layers'][0]['cover']+str(x['obs_layers'][0]['base']),'forecast':'SCT5000'} for x in r['case'] if 12<=x['local_hour']<=16]}]}
    ]}
    (HERE/'sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2))
    print(json.dumps(dict(zuuu=zu,current=current),ensure_ascii=False))


if __name__=='__main__':main()
