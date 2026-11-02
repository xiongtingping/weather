import json,hashlib,sys
from pathlib import Path
import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
HERE=Path(__file__).resolve().parent

def main():
    labels={'current':'Current','blend25':'25:75','blend50':'50:50','blend75':'75:25'}
    nb=nbformat.v4.new_notebook();nb.metadata.kernelspec={'name':'python3','display_name':'Python 3','language':'python'}
    nb.cells=[nbformat.v4.new_markdown_cell('# 冻结三个 Pareto 候选\n\n## tl;dr\n515 条共同样本上，25:75 的 Brier/LogScore 最好；REL/RES 细排随分档变化。找到 152 条首份可执行盘口、42 条新鲜权益记录，仍未绑定完整费用与执行合同，实际 EV / Log-growth 不可识别。'),
        nbformat.v4.new_markdown_cell('## Context & Methods\n固定上一轮 25/50/75% 中心权重份额，未重训或择优分母。\n\n### Key Assumptions\n本窗已参与设计，为 retrospective-asof，不是独立未见 OOS；33 站、23 日、515 条。线性池未经过最终服务后处理。经济缺失不填零。'),
        nbformat.v4.new_code_cell("import json, hashlib,sys\nfrom pathlib import Path\nbase=Path.cwd();sys.path.insert(0,str(base))\nimport evaluate as E\nguard=E.C.study.Guard()\nr=json.loads((base/'results.json').read_text())\nv=json.loads((base/'validation.json').read_text())\nf=json.loads((base/'candidates.json').read_text())\nfor name,sha in r['input_sha256'].items():\n    assert hashlib.sha256((E.C.ROOT/name).read_bytes()).hexdigest()==sha\nassert r['sample_n']==f['sample_n']==515\nprint('515 station-days; 33 stations; 23 dates; all rows have 11 buckets')"),
        nbformat.v4.new_markdown_cell('## Data\n冻结权重 hash 和共同样本 hash 在 candidates.json；逐样本评分在 scores.json。所有候选使用相同 515 条。'),
        nbformat.v4.new_code_cell("labels="+repr(labels)+"\nprint(f\"{'Candidate':10} {'Brier':>10} {'LogScore':>10} {'REL':>12} {'RES':>12}\")\nfor a,label in labels.items():\n    x=r['arms'][a];d=x['decomposition']['primary']\n    assert abs(d['reliability']-d['resolution']+d['uncertainty']+d['binning_correction']-x['mean_bucket_brier'])<1e-12\n    print(f\"{label:10} {x['brier']:10.6f} {x['logscore']:10.6f} {d['reliability']:12.8f} {d['resolution']:12.8f}\")\nprint('Independent scoring checks:',v['independent_brier_and_logscore_n'])"),
        nbformat.v4.new_markdown_cell('## Results\n主分档的校准图；点为各档加权平均预测与经验频率。分档并不提供独立 OOS 校准证明。'),
        nbformat.v4.new_code_cell("import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nfrom IPython.display import display,Image\nplt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})\nfig,ax=plt.subplots(figsize=(7,5))\ncolors=['#555555','#197A6A','#235D91','#B57935']\nfor (a,label),color in zip(labels.items(),colors):\n    bs=[b for b in r['arms'][a]['reliability_bins'] if b['event_weight']>0]\n    ax.plot([b['mean_probability'] for b in bs],[b['observed_frequency'] for b in bs],marker='o',markersize=4,label=label,color=color)\nax.plot([0,1],[0,1],'--',color='#999999',linewidth=1)\nax.set(xlim=(0,1),ylim=(0,1),xlabel='Mean forecast probability',ylabel='Observed bucket frequency',title='Bucket reliability: fixed bins\\n515 station-days | 33 stations | retrospective comparison')\nax.legend();fig.tight_layout();fig.savefig(base/'reliability.png',dpi=150);plt.close(fig)\ndisplay(Image(filename=str(base/'reliability.png')))"),
        nbformat.v4.new_markdown_cell('主分档偏好 25:75 的 Reliability；20 档则略偏好 75:25。不可按本窗挑分档宣布赢家。'),
        nbformat.v4.new_code_cell("for bins in ['primary','deciles','twentieths']:\n    print(bins,{a:(round(r['arms'][a]['decomposition'][bins]['reliability'],8),round(r['arms'][a]['decomposition'][bins]['resolution'],8)) for a in labels})\nprint('Brier delta 95% block intervals:',{a:r['arms'][a]['delta_ci95']['brier'] for a in labels})"),
        nbformat.v4.new_markdown_cell('### 可兑现价值的证据覆盖\n固定发布后 300 秒上限，取第一份已到达且有效的盘口；未按更优价格选择。下图为资格覆盖，其中各中间数量不自动等于全部条件交集。'),
        nbformat.v4.new_code_cell("a=json.loads((base/'execution_audit.json').read_text())\nfee=json.loads((base/'nested_fee_audit.json').read_text())\nassert a['economic_status']=='NOT_IDENTIFIED'\nprint(a['counts']);print(a['capability_counts']);print('Nested fee fields found on scopes:',fee['scopes_with_any_fee_field'])\nfig,ax=plt.subplots(figsize=(9,4.3))\nnames=['Frozen cohort','First valid quote','Fresh equity','Quote + state + equity + registry','Complete economic evidence']\nvalues=[515,152,42,28,0]\nax.barh(names,values,color=['#718096','#235D91','#235D91','#235D91','#B57935']);ax.invert_yaxis();ax.set_xlim(0,590)\nfor i,value in enumerate(values):ax.text(value+6,i,str(value),va='center')\nax.set_xlabel('Station-days');ax.set_title('Economic evidence does not yet identify realizable returns')\nfig.tight_layout();fig.savefig(base/'economic_coverage.png',dpi=150);plt.close(fig)\ndisplay(Image(filename=str(base/'economic_coverage.png')))"),
        nbformat.v4.new_markdown_cell('## Takeaways\n三个候选保持冻结。概率比较完成，实际 EV/Log-growth 无数值可报。下一步先绑定历史费用/执行合同与共同资本路径，复现完整服务链，再在未参与设计的历史外层比较。来源、算法及缺口见 REPORT.md、ECONOMIC_CONTRACT.md 和各审计 JSON。')]
    km=KernelManager(kernel_name='python3');km.kernel_spec.argv=[sys.executable,'-m','ipykernel_launcher','-f','{connection_file}']
    NotebookClient(nb,timeout=180,km=km,resources={'metadata':{'path':str(HERE)}}).execute();nbformat.validate(nb);nbformat.write(nb,HERE/'analysis.ipynb')
    r=json.loads((HERE/'results.json').read_text());a=json.loads((HERE/'execution_audit.json').read_text())
    receipt={'schemaVersion':1,'items':[{'id':'pareto-probability','title':'冻结三候选的 Brier、LogScore 与校准分解','queries':[{'id':'fixed-cohort','source':{'label':'冻结权重的 515 条共同历史样本','files':[{'label':f} for f in ['candidates.json','results.json','validation.json']],
        'metricDefinitions':[{'label':'Bucket Brier','definition':'每站日全部桶的平方概率误差之和，再按站日平均，越低越好。'},{'label':'LogScore','definition':'真值桶概率的平均自然对数，越高越好，等于 NLL 的相反数。'},{'label':'REL/RES','definition':'固定概率分档、one-vs-rest 平均单桶 Brier 分解；包含分档修正项。'}],
        'filters':['33 个非 ASOS 站','同一批 515 条','中心份额固定 25%、50%、75%','未重训、未按结果删样本'],
        'caveats':['历史已参与候选设计，不是独立未见 OOS。','CRPS/Brier/LogScore 为后处理前线性池。','Reliability/Resolution 的候选排序对分档粒度敏感。','LogScore 不是独立于 NLL 的新证据。'],
        'evidenceFlow':[{'kind':'validation','title':'共同分母和分解核验','detail':'2060 项 Brier/LogScore 独立评分核对通过；分解最大重构误差 5.56e-17；候选权重 hash 未变。'}]},
        'reportingPeriod':'2026-08-28 至 2026-09-19；23 日',
        'columns':['候选','Bucket Brier','LogScore','Reliability','Resolution'],
        'rows':[{'候选':label,'Bucket Brier':round(r['arms'][arm]['brier'],6),'LogScore':round(r['arms'][arm]['logscore'],6),'Reliability':round(r['arms'][arm]['decomposition']['primary']['reliability'],8),'Resolution':round(r['arms'][arm]['decomposition']['primary']['resolution'],8)} for arm,label in labels.items()],
        'methods':[{'language':'calculation','code':'BS = mean_station_day(sum_bucket((p-y)**2))\nLogScore = mean(log(p_truth))\nMean-bucket BS = Reliability - Resolution + Uncertainty + binning_correction'}]}]},
        {'id':'pareto-economics','title':'实际 EV 和 Log-growth 是否已有完整证据？','queries':[{'id':'economic-evidence','source':{'label':'官方 WS 与历史资本/状态证据审计','files':[{'label':f} for f in ['audit_economics.py','audit_execution.py','execution_audit.json','nested_fee_audit.json','ECONOMIC_CONTRACT.md']],
        'filters':['相同 515 个预测站日','发布后最多等待 300 秒','第一份已到达且有效的官方快照','同一规则适用于三个候选'],
        'caveats':['费用、执行合同和统一资本路径未全部绑定，经济结果不可识别。','缺失收益不是零收益。','候选自报 EV 不能证明实际盈利。','现有 archive WS 缺少当前 canonical 合同必需字段。'],
        'evidenceFlow':[{'kind':'method','title':'逐时点资格审查','detail':'检查预测、snapshot、quote、逐桶 book、created 和 cash 的可用时钟，以及 quote TTL；不挑有利价格。'},{'kind':'validation','title':'补查嵌套费用字段','detail':'对可用 registry 及对应 shadow 回执递归检查费用字段，仍未形成可绑定历史费用合同。'}]},
        'reportingPeriod':'与概率研究相同的 515 个站日',
        'columns':['证据阶段','站日数'],'rows':[{'证据阶段':k,'站日数':n} for k,n in [('共同样本',515),('300 秒内首份完整盘口',152),('新鲜账户权益记录',42),('盘口、库存、订单、权益、registry 交集',28),('完整经济证据交集',0)]]}]}]}
    (HERE/'sources_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    files={p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in HERE.iterdir() if p.is_file() and p.name!='manifest.json'}
    (HERE/'manifest.json').write_text(json.dumps({'status':'PROBABILITY_COMPARISON_COMPLETE_ECONOMICS_NOT_IDENTIFIED','production_writes':0,'notebook_executed_cells':sum(c.cell_type=='code' for c in nb.cells),'files':files},indent=2)+'\n')
    print('Notebook executed; two figures, sources and manifest written')

if __name__=='__main__':main()
