import json,hashlib,sys
from pathlib import Path
import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
HERE=Path(__file__).resolve().parent

def main():
    r=json.loads((HERE/'results.json').read_text())
    labels={'current':'Current','center_stack21':'Center + ridge','crps_hier21':'Hierarchical CRPS','blend50':'50:50 blend','joint':'Joint objective','joint_guarded':'Joint + training guards'}
    nb=nbformat.v4.new_notebook();nb.metadata.kernelspec={'name':'python3','display_name':'Python 3','language':'python'}
    nb.cells=[nbformat.v4.new_markdown_cell('# 中心与分布优势组合\n\n## tl;dr\n50:50 在相同的 515 个历史站日上实现 RMSE −0.380%、CRPS −0.280%、NLL −0.210%。三项区间仍跨零，前半窗 NLL 略差；研究候选，生产不动。'),
        nbformat.v4.new_markdown_cell('## Context & Methods\n固定 25/50/75% 组合、三指标联合目标、训练不劣约束；50:50 为计算前指定主要组合。逐时点训练只使用已知标签。\n\n### Key Assumptions\n已查看历史上的探索性追加；不是独立盲测。33 个非 ASOS 站；概率指标仅是后处理前线性池；总体改善不是每站改善。'),
        nbformat.v4.new_code_cell("import json, hashlib, sys\nfrom pathlib import Path\nbase=Path.cwd();sys.path.insert(0,str(base))\nimport joint as J\nguard=J.C.study.Guard()\nr=json.loads((base/'results.json').read_text())\nrows=json.loads((base/'scores.json').read_text())\nfor name,sha in r['inputs_sha256'].items():\n    assert hashlib.sha256((J.C.ROOT/name).read_bytes()).hexdigest()==sha\nassert len(rows)==515\nprint('515 paired station-days; 33 stations; 23 dates')"),
        nbformat.v4.new_markdown_cell('## Data\n输入与上一轮相同，全部三指标统一用具有恢复分布的 515 条；上轮 RMSE 的 604 条不用于本表。'),
        nbformat.v4.new_code_cell("v=json.loads((base/'validation.json').read_text())\nfor key in ['jensen_checks','cdf_check_n','cdf_max_error','joint_gradient_max_error','target_label_mutation_invariant','zero_hit_n']:\n    print(key, v[key])"),
        nbformat.v4.new_markdown_cell('## Results\n比较基线为现行权重。百分比小于零为改善。'),
        nbformat.v4.new_code_cell("labels="+repr(labels)+"\nprint(f\"{'Candidate':26} {'RMSE':>10} {'CRPS':>10} {'NLL':>10}\")\nfor arm,label in labels.items():\n    m=J.metrics(rows,arm)\n    for metric in ['rmse','crps','nll']:assert abs(m[metric]-r['arms'][arm][metric])<1e-12\n    print(f\"{label:26} {m['rmse']:10.6f} {m['crps']:10.6f} {m['nll']:10.6f}\")"),
        nbformat.v4.new_code_cell("import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nimport numpy as np\nfrom IPython.display import display, Image\nplt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})\narms=[a for a in labels if a!='current']; y=np.arange(len(arms))\nfig,axes=plt.subplots(1,3,figsize=(13,4.5),sharey=True)\nfor ax,metric in zip(axes,['rmse','crps','nll']):\n    d=np.array([r['arms'][a]['change_pct'][metric] for a in arms])\n    ci=np.array([r['arms'][a]['delta_ci95'][metric] for a in arms])*100/r['arms']['current'][metric]\n    ax.hlines(y,ci[:,0],ci[:,1],color='#557A95',linewidth=2)\n    ax.scatter(d,y,color=['#557A95','#557A95','#BC6C25','#557A95','#557A95'],zorder=3)\n    ax.axvline(0,color='#666',linewidth=1);ax.set_title(metric.upper());ax.set_xlabel('Change vs current (%)')\naxes[0].set_yticks(y,[labels[a] for a in arms]);axes[0].invert_yaxis()\nfig.suptitle('50:50 improves all three historical averages; uncertainty remains\\n515 paired cases | 33 stations | Aug 28 - Sep 19',fontsize=13)\nfig.tight_layout(rect=(0,0,1,.90));fig.savefig(base/'joint_comparison.png',dpi=150);plt.close(fig)\ndisplay(Image(filename=str(base/'joint_comparison.png')))"),
        nbformat.v4.new_markdown_cell('三日块 bootstrap 的三项区间均跨零。50:50 的均值改善，不能解读成已证明未来三降。'),
        nbformat.v4.new_code_cell("b=r['arms']['current'];m=r['arms']['blend50']\nfor half,x in m['halves'].items():\n    print(half,{k:round(100*(x[k]/b['halves'][half][k]-1),4) for k in ['rmse','crps','nll']})\nprint('Stations with all three lower:', m['station_all_three_lower_n'],'/33')\nprint('Leave-one-station-out ranges:',m['leave_one_station_out_delta_range'])"),
        nbformat.v4.new_markdown_cell('## Takeaways\n保留 50:50 为下一主要候选。冻结比例，复现完整服务链并使用未参与本轮设计的历史外层验证；未来观察只验证接口/到达，不算 OOS。来源与限制见 REPORT.md / PROTOCOL.md；本目录脚本可重算，manifest 记录哈希。')]
    km=KernelManager(kernel_name='python3');km.kernel_spec.argv=[sys.executable,'-m','ipykernel_launcher','-f','{connection_file}']
    NotebookClient(nb,timeout=180,km=km,resources={'metadata':{'path':str(HERE)}}).execute();nbformat.validate(nb);nbformat.write(nb,HERE/'analysis.ipynb')
    receipt={'schemaVersion':1,'items':[{'id':'joint-weight-gains','title':'组合能否让 RMSE、CRPS、NLL 同时下降？','queries':[{'id':'joint-frozen-comparison','source':{
        'label':'冻结日前包的中心与概率权重组合研究','files':[{'label':f} for f in ['PROTOCOL.md','joint.py','results.json','validation.json']],
        'metricDefinitions':[{'label':'共同分母','definition':'三指标均使用相同 515 个站日，原权重作为共同基线。'},{'label':'CRPS/NLL','definition':'对恢复的单系统概率作线性混合，尚未经过最终服务后处理。'}],
        'filters':['33 个非 ASOS 站','当时已知标签训练','固定 50:50 主要组合','相同模型集合、家族上限及市场桶几何'],
        'caveats':['该历史窗已参与端点方案研究，不是新的独立盲测。','三项 95% 日期块区间均跨零。','前半窗 50:50 的 NLL 变差约 0.0266%，尚未稳定跨时段三降。','只有 11/33 站各自实现三降。','概率结果不是最终 P0/P0-M/P1 服务表现。'],
        'evidenceFlow':[{'kind':'method','title':'冻结候选','detail':'计算前固定三组凸组合及两项联合目标；50:50 为主要候选，未按外层择优比例。'},{'kind':'validation','title':'数值与时点','detail':'4635 项凸性检查、4120 项独立积分评分通过；联合目标梯度、预测标签污染不变性、家族上限与训练不劣约束复核通过。'}]},
        'reportingPeriod':'2026-08-28 至 2026-09-19；515 条、33 站、23 日',
        'columns':['方案','RMSE变化百分比','CRPS变化百分比','NLL变化百分比'],
        'rows':[{'方案':label,'RMSE变化百分比':round(r['arms'][a]['change_pct']['rmse'],3),'CRPS变化百分比':round(r['arms'][a]['change_pct']['crps'],3),'NLL变化百分比':round(r['arms'][a]['change_pct']['nll'],3)} for a,label in labels.items()],
        'methods':[{'language':'calculation','code':'w_blend = 0.5*w_center + 0.5*w_hierarchical\np_blend = sum_model(w_blend * p_model)\nRMSE = sqrt(mean(center_error**2))\nCRPS = sum_i p_i*abs(x_i-y) - 0.5*sum_ij p_i*p_j*abs(x_i-x_j)\nNLL = mean(-log(p_truth_bucket))'}]}]}]}
    (HERE/'sources_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    files={p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in HERE.iterdir() if p.is_file() and p.name!='manifest.json'}
    (HERE/'manifest.json').write_text(json.dumps({'status':'EXPLORATORY_NOT_PROMOTED','production_writes':0,'notebook_executed_cells':sum(c.cell_type=='code' for c in nb.cells),'files':files},indent=2)+'\n')
    print('Executed notebook and receipt ready')

if __name__=='__main__':main()
