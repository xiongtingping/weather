from common import *
a=json.loads((OUT/'analysis.json').read_text());v=json.loads((OUT/'verification.json').read_text());m=json.loads((OUT/'manifest.json').read_text());labels=json.loads((OUT/'label_audit.json').read_text());official=json.loads((OUT/'official_sources.json').read_text())
period='2026-09-09 至 2026-09-11；每站当地 08/12/16/20/23 时'
def q(qid,label,files,rows=None,columns=None,**kw):
    value={'id':qid,'source':{'label':label,'files':[{'label':f} for f in files],**kw}}
    if rows is not None:value.update(rows=rows,columns=columns)
    return value
items=[]
items.append({'id':'p1-stale-state','title':'新鲜 P1 仍使用旧温度状态','queries':[{
 **q('p1-samples','P1 SSOT 与原始观测',['p1_compiler.py','production_worker.py','features_audit.json'],
 rows=[{'metric':'固定时点','count':675,'base':675},{'metric':'reaffirmation','count':284,'base':675},{'metric':'16 时 drawdown=0','count':119,'base':135},{'metric':'16 时回落幅度错位 >0.5°C','count':52,'base':135},{'metric':'20 时回落幅度错位 >0.5°C','count':116,'base':135}],columns=['metric','count','base'],
 sql=m['queries']['envelopes'],executedAt=m['asof'],tables=[{'name':'basket_probability_envelope'},{'name':'reported_trajectory_observations'}],
 metricDefinitions=[{'label':'回落幅度','definition':'同一站日截至当时已收到的 AWC 最高温减最新温度；与服务四层特征比较。'}],filters=['范围：45 站、135 个站日','P1：取样前已提交的 COMMITTED 分布'],caveats=['固定时点用于审查覆盖，不能当独立气象样本。','代码同时存在无新高续期及重新定根下的旧前缀计算。']),
 'reportingPeriod':period}]})
arms=v['hour16_fresh']['arms'];metrics=[{'metric':name,'served':arms['old'][key],'candidate':arms['updated'][key]} for key,name in [('hit','落桶命中率'),('mae','中心绝对误差°C'),('nll','负对数损失'),('brier','多类别 Brier')]]
items.append({'id':'phase-refresh-experiment','title':'刷新阶段输入的离线候选效果','queries':[{
 **q('phase-experiment','闭日预测与阶段刷新实验',['analyze.py','verification.json'],rows=metrics,columns=['metric','served','candidate'],
 metricDefinitions=[{'label':'候选','definition':'在固定 hazard artifact 上只刷新 phase 的时钟、回落、动量、云量和降水，保留区域协变量、上方形状与末端概率地板。'}],filters=['取样：当地 16 时','样本：通过原新鲜度门的 89 个站日','排除：更正日、未应用 phase、已出现新高但基线未反映的时点'],caveats=['候选筛查不是全域 strict-as-of OOS，不支持直接晋级或交易收益结论。','08 时组未改善；不能把下午收益推广到全天。','从已过地板的 served 分布重组与完整生产重放略有差异；16 时恒等组 NLL 0.952，对比原服务 0.953。']),
 'reportingPeriod':'2026-09-09 至 2026-09-11；当地 16 时'}]})
items.append({'id':'bucket-label-mismatch','title':'升温标签与结算桶 crossing 不一致','queries':[{
 **q('label-audit','phase 训练样本与根桶几何',['build_metar_phase_hazard_samples.py','label_audit.py','label_audit.json'],rows=[{'unit':'F','samples':6522,'mismatch':230},{'unit':'C','samples':33359,'mismatch':53}],columns=['unit','samples','mismatch'],sql=labels['query'],tables=[{'name':'metar_phase_hazard_sample'},{'name':'basket_probability_envelope'}],metricDefinitions=[{'label':'错位','definition':'round(truth−running_high)≥1 与 truth 是否进入 running_high 桶之上不一致；桶区间来自对应根的真实 registry 几何。'}],caveats=['C 站也包括开口尾桶等边界情形。']),
 'reportingPeriod':'2026-08-10 至 2026-09-06；39,881 条训练样本'}]})
lat=a['latency']
items.append({'id':'observation-latency','title':'接收长尾与内部处理耗时','queries':[{
 **q('latency-audit','AWC 观测与来源轮询',['latency_detail.json','analysis.json'],rows=[{'interval':name,'p50_seconds':lat[key]['p50'],'p90_seconds':lat[key]['p90']} for key,name in [('valid_to_seen_seconds','valid→first_seen'),('request_seconds','fetch_started→fetch_completed'),('seen_to_commit_seconds','first_seen→commit')]],columns=['interval','p50_seconds','p90_seconds'],sql=m['queries']['observations'],tables=[{'name':'reported_trajectory_observations'},{'name':'reported_trajectory_source_polls'}],filters=['样本：4,637 条无更正标记 AWC METAR/SPECI'],caveats=['valid→first_seen 包含发布、转发、轮询及补采，不是纯网络时延。','没有逐事件配对市场价格迁移；无法据此量化市场领先分钟数。']),
 'reportingPeriod':'2026-09-09 至 2026-09-11'}]})
links=[{'label':'AWC API','url':'https://aviationweather.gov/data/api/'},{'label':'AWC METAR 文档','url':'https://aviationweather.gov/help/data/'},{'label':'NWS ASOS 用户指南','url':'https://www.weather.gov/media/asos/aum-toc.pdf'}]
items.append({'id':'metar-best-practice','title':'METAR 时间、精度与 SPECI 语义','queries':[{'id':'official-documents','source':{'label':'AWC / NWS 官方文档','links':links,'caveats':['ASOS 用户指南为历史技术文档，当前逐源产品语义仍需核实。','未读取成功的 FMH1 不作为证据。']},'summary':'SPECI 的触发不保证覆盖普通升温；温度报文存在舍入与平均窗口；6 小时及日最高温组需要匹配观测窗口与站点结算日。','reportingPeriod':'官方网页于 2026-09-12 读取'}]})
save('sources_receipt.json',{'schemaVersion':1,'items':items})
