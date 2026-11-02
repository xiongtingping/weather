"""Build and execute a compact notebook and source receipt from frozen outputs."""
import json,hashlib,os,sys
from pathlib import Path
import nbformat
from nbclient import NotebookClient
HERE=Path(__file__).resolve().parent

def main():
    labels={'current':'Current','equal':'Equal','station3':'Station 3d','station5':'Station 5d','station5_shrink':'Station 5d + shrink','station_lead_eb21':'Station / lead MSE','center_stack21':'Center stacking','crps_hier21':'Hierarchical CRPS*'}
    r=json.loads((HERE/'results.json').read_text());b=r['arms']['current']
    nb=nbformat.v4.new_notebook();nb.metadata.kernelspec={'display_name':'Python 3','language':'python','name':'python3'}
    cells=[nbformat.v4.new_markdown_cell('# 多系统融合权重：冻结历史比较\n\n## tl;dr\n近 3/5 日直接加权未证明改善概率；5 日中心 RMSE 降约 0.40%，线性池 CRPS 却升约 0.34%。生产保持。分层 CRPS 是下一研究方向，尚未通过晋级。'),
      nbformat.v4.new_markdown_cell('## Context & Methods\n33 个非 ASOS 站；08-28 至 09-19，604 条中心评分，515 条恢复分布评分。所有候选逐时点只读已知标签。\n\n### Key Assumptions\n历史 TAF 可用性筛选；不是全 45 站；CRPS 是市场桶代表点的线性池评分，不是最终服务。外层曾被研究查看，分层 CRPS 为后加探索臂。详见 PROTOCOL.md / REPORT.md。'),
      nbformat.v4.new_code_cell("import json, sys, hashlib\nfrom pathlib import Path\nbase=Path.cwd()\nsys.path.insert(0,str(base))\nfrom compare import study, summarize\nguard=study.Guard()\nr=json.loads((base/'results.json').read_text())\nrows=json.loads((base/'scores.json').read_text())\nv=json.loads((base/'validation.json').read_text())\nfor name,sha in r['inputs_sha256'].items():\n    assert hashlib.sha256((study.ROOT/name).read_bytes()).hexdigest()==sha\nassert len(rows)==r['n']==604\nprint('33 stations / 604 center cases / 515 distribution cases / 23 days')"),
      nbformat.v4.new_markdown_cell('## Data\n输入为原服务中心、历史成员与参数 hash；无包和 provisional 样本不伪造。'),
      nbformat.v4.new_code_cell("a=json.loads((base/'reconstruction_audit.json').read_text())\nprint(a['counts'])\nprint('Independent CRPS checks:', v['independent_crps_cases'])\nprint('Max integration discrepancy:', v['independent_crps_max_error'])"),
      nbformat.v4.new_markdown_cell('## Results\n负 CRPS 差表示比原权重线性池好。区间为连续 3 日 moving-block bootstrap 95% 描述区间；多候选、短历史，不能视为晋级检验。'),
      nbformat.v4.new_code_cell("labels="+repr(labels)+"\nb=r['arms']['current']\nprint(f\"{'Arm':26} {'RMSE C':>9} {'Pool CRPS':>10} {'NLL':>9}\")\nfor arm,label in labels.items():\n    s=summarize(rows,arm)\n    assert s['rmse']==r['arms'][arm]['rmse']\n    print(f\"{label:26} {s['rmse']:9.4f} {s['pool_crps']:10.6f} {s['pool_nll']:9.6f}\")"),
      nbformat.v4.new_code_cell("import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nimport numpy as np\nfrom IPython.display import display, Image\nplt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False})\narms=[a for a in labels if a!='current']; y=np.arange(len(arms))\nd=np.array([r['arms'][a]['pool_crps']-b['pool_crps'] for a in arms])\nci=np.array([r['arms'][a]['pool_crps_delta_ci'] for a in arms])\nfig,ax=plt.subplots(figsize=(10,5.5))\nax.hlines(y,ci[:,0],ci[:,1],color='#557A95',linewidth=2)\nax.scatter(d,y,color='#143D59',zorder=3)\nax.axvline(0,color='#555555',linewidth=1)\nax.set_yticks(y,[labels[a] for a in arms]);ax.invert_yaxis()\nax.set_xlabel('CRPS difference vs current-weight pool (C); left is better')\nax.set_title('Weight candidates: probability gains remain uncertain\\n515 paired cases | 33 stations | Aug 28 - Sep 19')\nfig.text(.25,.02,'* Hierarchical CRPS was added after the first pass; exploratory only.',fontsize=9)\nfig.tight_layout(rect=(0,.04,1,1));fig.savefig(base/'crps_comparison.png',dpi=150);plt.close(fig)\ndisplay(Image(filename=str(base/'crps_comparison.png')))"),
      nbformat.v4.new_markdown_cell('小幅改善候选的区间均跨零。近 3/5 日直接加权并未给出稳定概率优势。'),
      nbformat.v4.new_code_cell("arms=['current','station3','station5','station5_shrink','crps_hier21']\nvalues=[100*v['same_set_turnover'][a]['mean'] for a in arms]\nfig,ax=plt.subplots(figsize=(10,4))\nax.barh([labels[a] for a in arms],values,color=['#143D59','#BB7844','#BB7844','#557A95','#557A95'])\nax.invert_yaxis();ax.set_xlim(0,max(values)*1.22)\nfor i,value in enumerate(values):ax.text(value+.2,i,f'{value:.2f}%',va='center')\nax.set_xlabel('Mean half-L1 weight change (%)')\nax.set_title('Recent-only weights move more\\n276 adjacent available forecasts with the same model set')\nfig.tight_layout();fig.savefig(base/'weight_turnover.png',dpi=150);plt.close(fig)\ndisplay(Image(filename=str(base/'weight_turnover.png')))"),
      nbformat.v4.new_markdown_cell('## Takeaways\n保留现行权重；统一成绩账本、全队/提前量先验、站点收缩、短窗弱修正、最终服务 proper-score 验收。下一主线是补齐历史服务链复现与 45 站 canonical 标签；不通过新天气分档追本窗噪声。\n\nSources：samples.json（上轮冻结）、enrichment.json、reconstructed.json、scores.json、results.json、validation.json。来源 hash 在 manifest.json；重跑计算用 compare.py，独立校验用 validate.py。')]
    nb.cells=cells;nbformat.validate(nb)
    # Explicit interpreter avoids invoking a different installed Python kernel.
    from jupyter_client import KernelManager
    km=KernelManager(kernel_name='python3')
    km.kernel_spec.argv=[sys.executable,'-m','ipykernel_launcher','-f','{connection_file}']
    NotebookClient(nb,timeout=180,km=km,resources={'metadata':{'path':str(HERE)}}).execute()
    nbformat.write(nb,HERE/'analysis.ipynb')
    assert all(c.execution_count is not None for c in nb.cells if c.cell_type=='code')
    assert not any(o.output_type=='error' for c in nb.cells if c.cell_type=='code' for o in c.outputs)
    metrics=[{'方案':label,'中心RMSE_C':round(r['arms'][a]['rmse'],6),'线性池CRPS':round(r['arms'][a]['pool_crps'],6),'CRPS变化百分比':round(100*(r['arms'][a]['pool_crps']/b['pool_crps']-1),3)} for a,label in labels.items()]
    receipt={'schemaVersion':1,'items':[{'id':'fusion-weights','title':'近 3/5 日加权是否改善多系统融合？','queries':[{'id':'frozen-weight-comparison','source':{'label':'冻结日前包与历史参数的离线权重比较','files':[{'label':f} for f in ['PROTOCOL.md','compare.py','results.json','validation.json']],
        'metricDefinitions':[{'label':'中心 RMSE','definition':'604 个站日上加权单系统校准中心与官方真值的均方根误差，单位 °C。'},{'label':'线性池 CRPS','definition':'515 个配对站日，在后处理前混合历史成员分布，按实际市场桶代表点计算离散 CRPS。'}],
        'filters':['33 个非 ASOS 站','每站日一个日前包','训练只使用当时已知标签','候选保留同一行模型集合与家族约束'],
        'caveats':['概率指标不是最终 P0/P0-M/P1 服务指标。','既有样本按 TAF 可用性选样；不是全部 P0。','45 站闭环未完成；美国 ASOS 标签未进入主评分。','本窗已被研究查看；分层 CRPS 为后加探索臂，不能宣称独立盲测。','只有 23 个日期，小幅改善的日期块区间跨零。'],
        'evidenceFlow':[{'kind':'method','title':'时点与历史参数','detail':'冻结原服务中心；按历史 lineage hash 取参数，恢复中心与原值误差不超过 1e-7°C；原始包缺失者不重建。'},{'kind':'validation','title':'数值与泄漏检查','detail':'6695 个候选评分与独立 CDF 积分相符，最大误差 1.78e-15；不可知标签污染不改变权重；检查日历窗、家族约束与同模型集合周转。'}]},
        'reportingPeriod':'2026-08-28 至 2026-09-19；33 站、23 个日期',
        'columns':['方案','中心RMSE_C','线性池CRPS','CRPS变化百分比'],'rows':metrics,
        'methods':[{'language':'calculation','code':'CRPS = sum_i p_i * abs(x_i-y) - 0.5 * sum_ij p_i*p_j*abs(x_i-x_j)\nrecent_weight = normalize(1 / max(paired_MSE, 0.25)) subject to family caps\nshrink_weight = (1 - n/(n+10)) * current_weight + n/(n+10) * recent_weight'}]}]}]}
    (HERE/'sources_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in HERE.iterdir() if p.is_file() and p.name!='manifest.json'}
    (HERE/'manifest.json').write_text(json.dumps({'status':'RESEARCH_ONLY_NOT_PROMOTED','production_writes':0,'notebook_executed_cells':sum(c.cell_type=='code' for c in cells),'files':files},indent=2)+'\n')
    print('Executed notebook, two figures, receipt, manifest ready')

if __name__=='__main__':main()
