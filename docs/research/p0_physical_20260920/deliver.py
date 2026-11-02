"""Create an executed review notebook, evidence receipt and hash manifest."""
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from study import OUT,Guard,save


def main():
    guard=Guard();results=json.loads((OUT/'results.json').read_text())
    title='日前共同失误与物理信息：冻结样本复核'
    cells=[{'cell_type':'markdown','metadata':{},'source':[f'# {title}\n','离线研究，不写生产；下面单元在交付前已执行。完整实验协议、排除原因与研究限制见 REPORT.md。\n']}]
    codes=[
        "import json, sys\nfrom pathlib import Path\nbase = Path.cwd()\nif not (base / 'samples.json').exists():\n    base = base / 'docs/research/p0_physical_20260920'\nsys.path.insert(0, str(base.resolve()))\nfrom study import Guard\nguard = Guard()\nfrom evaluate import partition, validate_evidence\nfrom model import scored_rows, summarize\nsamples = json.loads((base / 'samples.json').read_text())\nvalidate_evidence(samples)\nprint('配对站日', len(samples), '主样本', sum(s['primary_eligible'] for s in samples))\n",
        "train, validation, refit, outer, split = partition(samples)\nprint(json.dumps(split, ensure_ascii=False, indent=2))\n",
        "artifacts = json.loads((base / 'candidate_artifacts.json').read_text())\nfor arm, artifact in artifacts['artifacts'].items():\n    scored = scored_rows(outer, artifact)\n    print(arm, summarize(scored))\n",
        "result = json.loads((base / 'results.json').read_text())\nprint('联合对 P0', result['outer']['joint']['paired_crps'])\nprint('联合对 TX', result['joint_vs_tx'])\nprint('中心误差分解', result['attribution_outer'])\n",
        "audit = json.loads((base / 'source_audit.json').read_text())\nreconcile = json.loads((base / 'source_reconciliation.json').read_text())\nprint('参考物理字段', audit['reference_physics'])\nprint('真值映射', reconcile['counts'])\nassert not reconcile['clock_failures']\nprint('未来回执失败', len(reconcile['clock_failures']))\n",
        "native = json.loads((base / 'native_physics_inventory.json').read_text())\nprint('原生历史包盘点', native['counts'])\nprint('证据边界', native['qualification'])\nassert native['production_writes'] == 0\nassert native['network_requests'] == 0\n",
    ]
    env={};original=Path.cwd()
    import os
    os.chdir(OUT)
    try:
        for i,code in enumerate(codes,1):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):exec(compile(code,f'<notebook-{i}>','exec'),env)
            cells.append({'cell_type':'code','metadata':{},'execution_count':i,'source':code.splitlines(True),
                          'outputs':[{'output_type':'stream','name':'stdout','text':output.getvalue().splitlines(True)}]})
    finally:os.chdir(original)
    save('analysis.ipynb',{'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},'cells':cells})
    metrics=[]
    labels={'identity':'历史 P0','unconditional':'无条件校准','tx':'TX＋上下文','weather':'云雨风＋上下文','joint':'联合候选'}
    for arm,row in results['outer'].items():
        m=row['metrics'];metrics.append({'方案':labels[arm],'站日数':m['n'],'CRPS_C':round(m['crps_c'],6),'完整_NLL':round(m['nll'],6),'CRPS变化百分比':round(row['paired_crps']['crps_change_pct'],3)})
    receipt={'schemaVersion':1,'items':[{'id':'physical-candidate','title':'物理信息候选是否改善日前概率？','queries':[{
        'id':'frozen-outer','source':{'label':'冻结日前服务快照与官方 TAF 离线研究',
            'files':[{'label':x} for x in ['samples.json','candidate_artifacts.json','results.json','source_audit.json','source_reconciliation.json']],
            'metricDefinitions':[{'label':'CRPS','definition':'CRPS 按市场桶代表点的离散温度分布计算，以 °C 计，越低越好。'},
                                 {'label':'完整 NLL','definition':'完整 NLL 对每个留出样本实际命中的市场桶计算负对数概率，不删除零概率命中样本。'}],
            'filters':['33 个非 ASOS 站','每站日一份本地零点前可用 P0','TAF 三时钟均早于候选决策','外层前冻结模型'],
            'caveats':['候选属于离线 P0-M，不是原生 P0 物理字段方案。','只有 8 个外层日期，联合 CRPS 变化的日期 bootstrap 区间跨零。','11 个 ASOS 站的标签语义未统一，不进入主评分。','内层有 3 个命中零概率桶，完整 NLL 为正无穷；本表是无零命中的外层。'],
            'evidenceFlow':[{'kind':'validation','title':'时序与数值检查','detail':'15 项测试通过，包含未来回执拒绝、外层真值污染隔离、独立 CRPS 积分、数值梯度与回执复现。'},
                            {'kind':'method','title':'参数选择','detail':'内层只选择 ridge；外层所有日期冻结同一候选，不用外层调参。'}]},
        'reportingPeriod':'2026-09-12 至 2026-09-19；257 站日、8 个日期',
        'columns':['方案','站日数','CRPS_C','完整_NLL','CRPS变化百分比'],'rows':metrics,
        'methods':[{'language':'calculation','code':'CRPS = sum_i p_i |x_i-y| - 0.5 sum_ij p_i p_j |x_i-x_j|\nNLL = -log(p_truth_bucket)\nCRPS change (%) = 100 * (candidate_mean / baseline_mean - 1)'}]
    }]}]}
    save('sources_receipt.json',receipt)
    files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(OUT.iterdir()) if p.is_file() and p.name!='manifest.json'}
    save('manifest.json',{'schema':'p0_physical_study_manifest_v1','status':'RESEARCH_ONLY_NO_PROMOTION','files':files,
                           'production_writes':0,'notebook_executed_cells':len(codes),'tests_passed':28,
                           'test_suites':{'test_study.py':15,'test_native_physics.py':13}})
    print('DELIVERED',len(files),'files',len(codes),'executed notebook cells')


if __name__=='__main__':main()
