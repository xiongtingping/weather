"""Package reviewed diagnosis, evidence and an inspectable notebook."""
import pathlib,json,math,collections,hashlib,sys,ast
P=pathlib.Path(__file__).resolve().parent;ROOT=P.parents[1]
a=json.loads((P/'analysis.json').read_text());s=json.loads((P/'serving_analysis.json').read_text());e=json.loads((P/'exact_bucket_screen.json').read_text())
alias={'DWD_ICON_GLOBAL_EPS':'ICON','ECCC_GEM_GLOBAL_ENS':'GEM','ECMWF_IFS_ENS_OPEN_PF50':'IFS','NOAA_GEFS_05':'GEFS'}
arms={'baseline5':'现行 5 天','spread15':'宽度半衰期 15 天','spread30':'宽度半衰期 30 天','rms_denominator5':'仅改 RMS 分母','variance_floor_mix15':'15 天＋方差地板'}
blocks=[]
def md(id,body,source=None):
    b={'id':id,'type':'markdown','body':body}
    if source:b['sourceId']=source
    blocks.append(b)
title='模型分布失准：中心、宽度与收敛诊断'
md('title','# '+title)
md('summary','''## 判断：短样本宽度估计是实际薄弱点，评估与服务不一致更应先修

熊先生，您的假设值得继续，而且本次保留站点、提前量和 cycle 的对照给出了支持。**5 天衰减同时驱动中心和宽度，尺度估计确实样本薄、反应快；但它不是目前能证明的唯一根因。**

更直接的问题在校准链自身：分布校准器在已经校准过的分布上再拟一遍，却按绝对参数发布；OOS 与生产的 band / 中心宽度顺序不同；开口桶的轴定义也不同。这些已确认缺陷会让“评估通过的分布”和“实际服务的分布”不一致。

优化顺序应是：**先统一拟合、评分、服务的数学合同；再将中心与尺度分开估计；最后用融合后的真实残差统一校准宽度。** 当前证据支持候选进入完整链路的严格历史测试，不支持直接宣称新参数已胜出或可以晋级。''')
md('scope','''## 样本和指标必须先对齐

本次只读核查截止于 2026 年 9 月 9 日 16:36:32 UTC，覆盖用户指定的 45 站。生产加载器返回 38,418 条候选历史记录，其中 37,790 条为四个官方原生系统记录，628 条保留 Open-Meteo republisher lineage；后者未进入本次 8,593 次宽度对照。AIFS 没有足够原生历史进入这项对照，不能把四系统结论包装为五系统都已验收。

长窗对照覆盖 8 月 20 日至 9 月 8 日，45 站、899 个站日；每个预测只用其事件日前 60 日内、至少 20 个更早事件日，保持中心算法不变。它使用审计时保留的历史 truth revision，**没有逐折重建历史 arrival / truth revision，因此是回溯时序诊断，不是 strict-as-of 晋级 OOS。**

服务快照另取 8 月 28 日、9 月 1 日、5 日、8 日：459 个 P0 分布、943 份引用的单系统 artifact；每站×事件日×提前量仅取最后一个已提交 P0。桶评分使用这些分布所携带的实际区间，C/F 不硬编码桶宽。

中心误差、σ、CRPS 全部以 ℃ 计算；NLL 是真实结算桶的负对数概率，越小越好；熵是桶概率的 Shannon entropy，以 nat 计。概率峰桶命中率与完整分布校准是不同目标。开口桶没有可识别的有限均值/方差，本文服务 σ 只作固定代表点下的代理量，同时检查边缘质量较低的样本。''','methods')
md('metrics','''## 原附件的方向有依据，几项推论不能直接成立

**σ 的百分比收缩不能减去熵的百分比收缩。** 对高斯位置尺度族，熵差约为 ln(σ短/σ长)；例如熵从 2.00 降至 1.94 nat 是下降 3%，对应尺度约缩小 5.8%，不是 3%。离散桶、开口尾桶、偏态和多峰还会改变这个近似。中心绝对误差的中位数下降，也不代表 RMSE、残差标准差或尾部风险按同一比例下降。

附件没有附原查询、逐行样本和四个切点明细，故 0.835→0.717、−3% 和 GEFS 的 3.7 倍尚未在相同分母上复现。本次更严格的同站×同日×同 cycle 配对中，原始 spread 确实随提前量缩短收敛；数值与附件不同是样本及配对口径不同，不能相互替代。

附件“池化输了，所以不是估计噪声”“粗分组仿射输了，所以函数形式没问题”都超出了实验能证明的范围。前者同时删除站点异质性，后者同时改变条件化；它们排除了那些替代实现，不能排除保留条件化的慢速尺度或方差地板。当前加载器还明确把滚动窗口内季节合并拟合、再贴季节标签，附件所谓逐季独立拟合也不符合当前实现。''','attachment')
md('raw','''## 原始集合会收敛，但它只描述自身成员的一致性

下图在同站、同事件日、同 run cycle 配对后比较原始成员 σ。IFS、GEFS、GEM 各覆盖 45 站；ICON 只有 16 站具备这个严格配对，不能把其结果扩展为 45 站结论。收敛方向成立，幅度不等于全部预测误差的收敛。

格点集合的成员散度没有自动覆盖站点代表性误差、持续偏差、偏差估计误差以及模型之间共同遗漏的天气过程。单靠更小的成员 σ 不能证明预测理应同幅度变窄。具体物理误差源需要配对原生时序、站点 truth 及天气条件后再定位，本次不把“模型欠离散”本身冒充它的物理根因。''','analysis')
blocks.append({'id':'raw-chart','type':'chart','chartId':'raw-spread'})
md('single','''## 单系统：中心与尺度共用短记忆，标量同时承受多个误差来源

现行中心为原始成员均值加偏差修正；偏差有温度三分位分箱并向本格总体偏差收缩。宽度估计仍使用总体偏差的残差，而非逐日样本外的分箱校准残差：`k = clip(SD加权(error−bias) / 平均加权(raw_sigma), 0.5, 5)`，当天成员偏离再乘 k。

对每天一条、5 天半衰期的无限历史，Kish 有效样本上限为 **14.45005**；缺日会更少，天气自相关还会进一步降低信息量。9 月 8 日实际引用参数的有效样本中位数分别为 IFS 11.90、GEFS 12.27、ICON 13.52、GEM 14.34；最薄的 IFS 格只有 4.22。它们真实训练行数可以达到数十条，不能与有效样本混淆。

在独立高斯近似下，以 14.45 的样本量估标准差，其相对标准误约 19.3%；估 k 还包含分母和中心估计的不确定性。但 k=3.7 不会再把这个相对误差机械放大 3.7 倍，放大的是绝对误差。小样本导致不稳定的机制成立，不能由此算出它占总损失的比例。

同一 8,593 次回溯预测中，单系统中心校准把样本平均 MAE 从 1.757℃ 降至 0.996℃，RMSE 从 2.247℃ 降至 1.363℃，说明中心校准有效；剩余中心误差仍需由样本外残差分析，而不能拿原始模型偏差要求宽度重复兜底。这组数是原始/校准中心比较，不是提前量收敛率，也不是最终融合中心成绩。

另一个结构限制是纯乘性：它假设模型越一致，全部残差风险就等比例趋近零。本次保留相同条件化的方差地板初筛更好，支持检验一个独立误差底座。分母用平均 σ 而不是 RMS σ 还使平均方差偏离目标，长窗中两者比值的中位数为 1.055、90 分位为 1.117；**仅修分母反而使 NLL 变差**，说明不能孤立地把公式改得“漂亮”就当作优化。

3.7 倍也必须放到同一格、同一时窗才可解释。独立检查的 GEFS h00_12 原生记录中，若只去掉全体总体偏差，残差 σ / 平均集合 σ 为 3.35；先去掉站点×cycle 的各自均值，则为 1.89。此对比不复现附件的 3.7，但证明跨格残差之比不能直接要求每格 k 都达到 3.7。''','methods')
md('fusion','''## 融合：中心误差减小，不会自动让混合分布同步变窄

当前融合已在有足够历史时使用协方差最小方差权重，不能再归因为“仍然只按 1/RMSE”。不过它优化的是中心残差权重，输出仍是分布混合及共识桥。

连续分布混合的方差为 `W+B`：W 是各系统方差的加权平均，B 是系统中心分歧的加权方差。它不是融合中心误差的 `wᵀΣw`。当前共识桥最多移走 40% 的分歧项，连续近似仍留下 `W+(1−β)B`；实际桶平移另有插值效应。

这并不说明混合分布在统计学上必然错误，而是暴露了要检验的预测语义：模型分歧代表不同真实情景，还是围绕同一真实值的噪声？如果属于后者居多，中心融合已经获益，但 W 和残留 B 仍让分布保留过多宽度；后续全局 λ 又会压缩本来很紧的样本。

协方差当前由过去原始中心误差、去总体均值、等权估计；偏差却按 5 天衰减且分箱。两个估计的残差对象和时钟不完全一致。长期方案应以逐日交叉拟合后残差决定权重，再用融合残差对 W、B 及估计不确定性校准，而不是直接规定越近必须越窄。''','code')
md('contract','''## 已证实的实现缺陷，应优先于任何参数寻优

**1. 分布校准重复作用，却按绝对参数换代。** `serving_cells_by_day/_serving_cell` 读取服务分布，只逆转 band，留下此前已应用的中心/宽度变换；`_cell_nll` 又对这个结果施加候选 λ。`fit_center_width` 以这种输入比较 incumbent / candidate，随后把候选 λ 直接作为新绝对值保存。连续近似中，“在已乘 0.75 的分布上再试 0.8”评价的是 0.60，发布却使用 0.8。桶插值不可逆，因此连简单乘回也不应视为完全等价。

**2. OOS 与服务的顺序相反。** OOS `_shaped` 为 band→中心宽度，生产 P0 为中心宽度→band。用 127 个真实服务分布作为相同起点、同一当前 band 表、相同 λ=0.75 作受控顺序实验，最大单桶差达到 6.32 个百分点。这是非交换性的见证，不是修复后的收益估计。

**3. 开口桶坐标不同。** 校准拟合器给开口桶取有限边界外 1℃，P0 服务取边界外 0.5℃；OOS 又使用整数 ℃ 网格。带尾部质量时，中心、方差、平移和缩放的对象不同。应保留连续温度分布到最后一步，以真实桶边界积分；诊断代理坐标则只能有一个 canonical 定义。

**4. 核平滑有不连续点。** 校准后 σ≤0.5℃ 时直接加 0.5℃ 高斯核；σ>0.5℃ 时先缩成员偏差来保方差。σ 从 0.500000 微增至 0.500001℃，连续输出 σ 反而从 0.707107 降至 0.500001℃。对称双成员样例中心桶概率从 47.72% 跳至 68.27%。长窗初筛有 428/8,593 个预测落在≤0.5℃这一侧，说明它不是完全不可达；该比例不是实际发生跳变的频率。''','code')
md('selection','''## 评估合同还存在选择乐观偏差

融合枚举模型子集，在 `common_holdout` 上选 CRPS 最佳者，又在同一 holdout 上报告胜者分数、作门控。即使权重没有回写训练集，**选子集本身也是拟合**。代码注释“没有反馈进 training 就没有 tune holdout”不成立。

分布层也在同一 forward 段上决定 pooled、floor、tier 哪个方案获准，缺少独立外层测试。它可用于开发选择，不能当作未参与选择的最终 OOS。20 train / 8 holdout 是基础资格门，不是免除多重选择偏差的保证；当前不少 no-live 参数仍使用更薄的 5+3 资格口径。

因此附件所称“每个替代都输，现行就是局部最优”最多是特定实现、特定评分口径下的观察。修复拟合/服务差异和验证复用以前，不能从这些负实验推出函数形式或估计方法已经排除。''','code')
md('experiments','''## 保留原条件化的新对照：先看真实桶，再看长窗辅助证据

下表来自四个预先选定闭日、45 站、180 个站日、1,866 次单系统预测：用真实成员、现行核平滑和保留的实际市场桶区间评分，每站日先平均其系统/提前量/cycle，再平均站日。正的 NLL / CRPS 改善表示更好，峰值桶变化以百分点计。

所有候选固定原中心算法。方差地板臂固定使用 `v = residual_SD15² × [0.5 + 0.5 × sigma_today² / E15(sigma²)]`，系数没有在这四个日期上寻优；保留每站×系统×提前量×cycle 的尺度。它是一项机制筛查，不是最终 EMOS 拟合，也没有经过融合与后续分布校准。''','exact')
blocks.append({'id':'experiment-table','type':'table','tableId':'experiments'})
md('experiment-interpretation','''单纯拉长半衰期改善 NLL，但 CRPS 基本不变、峰值桶命中下降；它主要减少了过度自信的惩罚，不能称为全面变准。带方差底座的候选在这个小窗口的合计中三项均改善，因而比“一律加大 k”更值得推进。折返检查发现它并非每个日期都赢：四日的 NLL 改善依次约为 +0.164、+0.072、+0.038、−0.006 nat，最近的 9 月 8 日反而略输；C/F 分组和三个提前量的合计均改善。**这正是暂不选定统一参数、必须做外层历史验证的理由。**

长窗高斯矩近似是另一项辅助检查：8,593 次预测中，15 天半衰期 NLL 改善 0.042 nat（34/45 站改善），方差地板臂改善 0.104 nat（39/45 站改善）；后者名义 95% 覆盖从 90.4% 到 93.8%。按事件日作 4 日块 bootstrap 的 NLL 改善区间分别约为 [0.024,0.063] 和 [0.078,0.130]，但只有 20 个事件日，天气相关及历史 revision 问题仍在。

这组对照支持“5 天尺度记忆与纯乘性结构值得改”，**不支持已经验证最终生产收益、尾部安全或晋级资格**。原始训练样本、Gaussian screen、真实单系统桶分布、最终服务分布是四种对象，不能把各自改善拼成一个总收益。''','methods')
md('convergence','''## 服务收敛必须固定系统、版本、站日和桶轴再判断

四日期服务快照里，同站同事件日的 155 对 h24_48→h00_12 分布，平均熵为 1.670→1.527 nat（−8.6%）。但不同日期的变化在 −4.3% 至 −19.5% 之间，模型入选集合和校准版本经常变化；只保留相同系统集合及相同中心宽度版本后仅余 14 对，熵约 −4.5%。即便这个子集，权重、单系统 artifact、天气状态仍未完全固定。

所以该快照不能给出纯粹的“校准传递率”，也不能用它推翻附件的 −3%。它证明的是：**目前缺少能把模型自然收敛、模型切换和校准换代分开的归因口径。** 未来验收需配对固定事件日、系统集合、算法版本和真实桶合同，同时记录每层中心、方差、80/95% 区间、熵及变换回执；按条件覆盖率决定是否应该更窄，而不是强制追随原始集合 σ。''','serving')
md('plan','''## 优化方案与可验证终点

**第一步：修数学合同。** 把训练评分与服务路由到同一套纯函数和唯一桶合同；固定“连续成员校准→融合→物理修正→最终分布校准→实际桶积分/硬事实处理”的明确顺序。分布校准必须从可重建的校准前分布拟合绝对参数，或显式定义残差校准并正确组合，不能混用。修核平滑断点；删除相互矛盾的重复实现。保存最小输入/输出 sha 与层级统计，不把只读诊断放入 SSOT 写者轮转。

验收：同输入、同参数版本、同桶合同下，训练、回放、服务逐桶概率一致；C/F、开口桶、跨格、参数换代及阈值前后往返都过；重复 refit 在已校准分布上应有稳定固定点，不能反复施加或反向撤销旧校正。

**第二步：中心快、尺度慢，保留站点条件化。** 中心先保留 5 天方案为基线，尺度对比 15/30 天并以独立历史选择；尺度残差应来自逐日交叉拟合后的中心，显式计入中心参数估计误差。检验 `sigma² = a + b·raw_sigma²` 或 Student-t / 残差核形式，a≥0，保留站点差异；在同站相邻提前量/cycle 之间做受约束的部分共享，输出仍绑定 exact activation。跨站完全池化不作为默认方案。有效样本、参数抖动和条件覆盖须一同验收。

**第三步：融合中心与融合宽度分别定标。** 用相同逐日样本外残差估计收缩协方差和权重，检验融合残差方差对 W、B 的关系，例如 `v_fused = a + bW + cB + v_estimation`；参数是否需要按提前量、站点共享由内层历史决定。保留 ≥2 独立家族约束，但不把“最小方差中心”误当成已校准概率分布。

**第四步：用独立外层历史结束争论。** 外层按时间向前滚动，内层完成参数、半衰期、子集和分布族选择；每折只读当时可得的原生对象、arrival/receipt、truth revision 和已激活版本。所有系统/来源严格保留 lineage；ASOS 11 站的训练 truth 与 canonical ASOS 优先/AWC 后备真值必须先对齐，当前加载器硬筛 AWC 这一差异需要单独确认。

最终接受依据：45 站逐站与全体配对的 CRPS、桶 NLL、偏差、MAE/RMSE、PIT/80/95% 条件覆盖；拆分 C/F、提前量、窄/宽集合、热/冷日及尾部；按站日/天气时间块给置信区间，并做删除单站、时间切点和来源敏感性。峰桶命中列作独立权衡，不允许拿熵越低或前向样本当 OOS。只有完整已闭日 strict-as-of 外层测试达标，才建立新的 exact artifact 和 no-live 验收。''')
md('limits','''## 已完成、尚未证明与下一个主线任务

已完成：45 站只读取证、单系统/融合/分布校准代码链核对、四日期服务配对、899 站日时序敏感性、180 站日实际桶核分布复核，以及 144 组跨 C/F 间距、2/10/50 成员、阈值两侧折返的质量守恒检查。分析器的中心与 k 实现对生产函数做了数值校验，最大绝对差约 1.3×10⁻¹⁵。

尚未证明：附件各数字的原始同分母复现；各缺陷对最终服务总损失的独立份额；AIFS 原生完整验证；候选穿过完整融合和后续校准后的 strict-as-of OOS 优势；站点代表性、采样时序等具体物理误差源。四个闭日的小窗口优势不等于稳定收益。

本次交付是可复查的根因诊断与优化方案，生产参数、SSOT 和常驻服务均未修改。**下一个主线任务：优先修复“分布校准重复作用＋拟合/服务順序和桶轴不一致”，再在统一评分链里验证中心/尺度分时钟与方差地板。**''')

raw=[]
for r in a['paired_raw_convergence']:
    for lead,key in [('h24_48','mean_long_sigma_c'),('h00_12','mean_short_sigma_c')]:
        raw.append({'system':alias[r['system']],'lead':lead,'sigma_c':r[key],'paired_scope_days':r['n'],'stations':r['stations'],
          'station_mean_relative_change':r['station_mean_relative_change'],'long_center_medae_c':r['median_abs_center_error_long_c'],'short_center_medae_c':r['median_abs_center_error_short_c']})
table=[{'rank':i,'method':arms[r['arm']],'nll_gain':r['nll_gain'],'crps_gain_c':r['crps_gain_c'],
         'peak_change_pp':r['peak_hit_change_pp'],'station_days':r['station_days']} for i,r in enumerate(e['summary'])]
sources=[{'id':'attachment','label':'用户附件 width_evidence.md（结论作为待核查主张）','path':'/Users/xiong/Downloads/width_evidence.md'},
 {'id':'analysis','label':'同站同日同 cycle 配对与时序敏感性','path':str(P/'analyze.py')},
 {'id':'serving','label':'保留的 P0 分布及 lineage 对照','path':str(P/'serving_analysis.py')},
 {'id':'exact','label':'真实市场区间的单系统成员核复核','path':str(P/'exact_bucket_screen.py')},
 {'id':'methods','label':'审计方法、计算与来源','path':str(P/'diagnosis.ipynb')},
 {'id':'code','label':'当前原生集合 OOS / P0 / center-width 实现及文件指纹','path':str(P/'source_fingerprints.json')}]
artifact={'surface':'report','manifest':{'version':1,'surface':'report','title':title,'generatedAt':'2026-09-09T16:36:32Z',
 'blocks':blocks,'sources':sources,'charts':[{'id':'raw-spread','type':'bar','title':'原始集合标准差：配对提前量比较','description':'单位 ℃；同站、同事件日、同 cycle。ICON 可配对站点仅 16 个。','showDescription':True,'dataset':'raw',
    'sourceId':'analysis','encodings':{'x':{'field':'system','type':'nominal'},'y':{'field':'sigma_c','type':'quantitative','label':'σ（℃）'},'color':{'field':'lead','type':'nominal'}},
    'options':{'grouping':'grouped','orientation':'vertical','legend':True}}],
 'tables':[{'id':'experiments','title':'宽度方案的实际桶评分','dataset':'experiments','sourceId':'exact',
   'columns':[{'field':'rank','label':'序号','type':'number'},{'field':'method','label':'方案','type':'string'},{'field':'nll_gain','label':'NLL 改善（nat）','type':'number'},
       {'field':'crps_gain_c','label':'CRPS 改善（℃）','type':'number'},{'field':'peak_change_pp','label':'峰桶命中变化（百分点）','type':'number'},{'field':'station_days','label':'站日','type':'number'}],
   'defaultSort':{'field':'rank','direction':'asc'}}]},
 'snapshot':{'version':1,'status':'ready','generatedAt':'2026-09-09T16:36:32Z','datasets':{'raw':raw,'experiments':table}},
 'package_info':{'description':'Read-only technical diagnostic snapshot; no production changes or promotion claim'}}
sys.path.insert(0,str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos
native_sql=(
 'SELECT ensemble_system_id,target_local_date,archive_lead_bucket,run_cycle,archive_query_at_utc,'
 'member_id,raw_tmax_c,content_sha256,transport_provider_id,native_run_init_utc,offline_oos_eligible,extraction_version '
 'FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND qc_status=\'ACCEPTED\' '
 'AND authority_scope=\'CALIBRATION_TRAIN_ONLY\' AND holdout_eligible=0 '
 f'AND transport_provider_id IN ({oos._OFFICIAL_TRANSPORT_SQL_LIST}) '
 'ORDER BY target_local_date,archive_query_at_utc,member_id;')
snapshot_sql=[]
for node in ast.walk(ast.parse((P/'snapshot.py').read_text())):
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='execute' and node.args and isinstance(node.args[0],ast.Constant):
        q=node.args[0].value
        if isinstance(q,str) and q.lower().startswith('select'):
            snapshot_sql.append(q+';')
for source in sources:
    if source['id'] in {'analysis','exact'}:
        source['query']={'engine':'SQLite','language':'sql','sql':native_sql+('\n\n'+'\n\n'.join(snapshot_sql) if source['id']=='exact' else ''),
          'description':'原始查询采用参数绑定逐站执行；成员完整性、truth as-of 与权限筛选由生产加载器处理，配对、时序拟合和最终计算见路径中的 Python 源码。此处保留原始查询而非伪造聚合 SQL。',
          'tables_used':['calibration_native_ensemble_historical_member_run','calibration_station_daily_high_truth','calibration_station_market_timezone_binding']+(['basket_probability_envelope','calibration_native_ensemble_system_artifact'] if source['id']=='exact' else []),
          'filters':{'station_universe':'用户指定45站，见manifest.json','as_of':'2026-09-09T16:36:32Z','scope':'native closed historical records; conditional pairing; republisher excluded from sensitivity'},
          'metric_definitions':{'sigma_c':'population standard deviation of raw native member daily maxima in Celsius','nll_gain':'mean per station-day of baseline bucket NLL minus candidate bucket NLL','crps_gain_c':'mean baseline minus candidate continuous Gaussian member-kernel mixture CRPS in Celsius'}}
(P/'source_queries.sql').write_text(native_sql+'\n\n'+'\n\n'.join(snapshot_sql))
for row in table:
    for key in ['nll_gain','crps_gain_c','peak_change_pp']:
        row[key+'_numeric']=row[key]
        row[key]=f"{row[key]:+.4f}"
for column in artifact['manifest']['tables'][0]['columns']:
    if column['field'] in ['nll_gain','crps_gain_c','peak_change_pp']: column['type']='string'
(P/'report_artifact.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2))
(P/'diagnosis.md').write_text('\n\n'.join(b['body'] for b in blocks if b['type']=='markdown'))
# Companion notebook: exact scripts are visible and runnable on demand, with
# resource-bounded scripts retaining ownership of all SSOT reads.
cells=[{'cell_type':'markdown','metadata':{},'source':['# 模型分布诊断复核\n','只读，历史 revision 按审计截点保留；所有筛查均不是晋级 OOS。\n','运行提取器会重新查询本地 SSOT；仅阅读已保存 JSON 不访问生产。']}]
def cell(code,output=None):
    cells.append({'cell_type':'code','execution_count':None,'metadata':{},'source':code.splitlines(True),'outputs':[] if output is None else [{'output_type':'stream','name':'stdout','text':[output]}]})
cell("from pathlib import Path\nimport json, math\np = Path("+repr(str(P))+")\nsummary = json.loads((p/'analysis.json').read_text())\nprint(summary['sensitivity'])\n",json.dumps(a['sensitivity'],ensure_ascii=False,indent=2))
cell("r = 2**(-1/5)\nneff = (1+r)/(1-r)\nprint(neff, 1/math.sqrt(2*(neff-1)))\n",'14.450047917745149 0.19280712836733696\n')
cell("bucket = json.loads((p/'exact_bucket_screen.json').read_text())\nprint(bucket['summary'])\n",json.dumps(e['summary'],ensure_ascii=False,indent=2))
cell("serving = json.loads((p/'serving_analysis.json').read_text())\nprint(serving['matched_serving_pairs'])\n",json.dumps(s['matched_serving_pairs'],ensure_ascii=False,indent=2))
for name in ['audit.py','snapshot.py','analyze.py','serving_analysis.py','exact_bucket_screen.py']:
    cells.append({'cell_type':'markdown','metadata':{},'source':['## '+name+'\n','完整可审计源码；下一格仅显示，不启动查询。']})
    text=(P/name).read_text()
    cell('print((p/'+repr(name)+').read_text())\n',text)
nb={'nbformat':4,'nbformat_minor':5,'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},'cells':cells}
(P/'diagnosis.ipynb').write_text(json.dumps(nb,ensure_ascii=False,indent=2))
print('report blocks',len(blocks),'datasets',len(raw),len(table),'bytes',(P/'report_artifact.json').stat().st_size)
