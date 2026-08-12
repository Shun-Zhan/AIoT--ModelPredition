import fs from "node:fs/promises";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const baseDir = new URL(".", import.meta.url).pathname;
const data = JSON.parse(await fs.readFile(`${baseDir}fusion_results.json`, "utf8"));
const wb = Workbook.create();
const navy="#12304A",teal="#0E7490",blue="#2563EB",green="#15803D",amber="#D97706",red="#B91C1C",pale="#EAF2F8",gray="#64748B";
const names={existing_proxy:"现有代理模型",field:"仅现场从头训练",mixed:"1:1平衡混合训练",transfer:"虹桥预训练+现场微调",existing:"现有N-BEATS",hongqiao_retrain:"虹桥重训",transfer_et0:"虹桥预训练+现场微调"};
const soilOrder=["existing_proxy","field","mixed","transfer"];
const horizons=[5,15,30,60];
function add(name){const s=wb.worksheets.add(name);s.showGridLines=false;return s;}
function title(s,text,end="P"){s.getRange(`A1:${end}1`).merge();s.getRange("A1").values=[[text]];s.getRange(`A1:${end}1`).format={fill:navy,font:{bold:true,color:"#FFFFFF",size:18},verticalAlignment:"center"};s.getRange("A1").format.rowHeight=34;}
function section(s,range,text){s.getRange(range).merge();s.getRange(range.split(":")[0]).values=[[text]];s.getRange(range).format={fill:teal,font:{bold:true,color:"#FFFFFF",size:12},verticalAlignment:"center"};}
function header(s,range){s.getRange(range).format={fill:pale,font:{bold:true,color:navy},borders:{preset:"inside",style:"thin",color:"#CBD5E1"},wrapText:true,verticalAlignment:"center"};}
function borders(s,range){s.getRange(range).format.borders={preset:"inside",style:"thin",color:"#E2E8F0"};}
function chart(s,type,source,pos,text,fmt="0.00"){const c=s.charts.add(type,s.getRange(source));c.title=text;c.titleTextStyle.fontSize=12;c.hasLegend=true;c.xAxis={axisType:"textAxis",textStyle:{fontSize:9}};c.yAxis={numberFormatCode:fmt};c.setPosition(...pos);return c;}
function fmt4(x){return Number(x).toFixed(4)}

// Create referenced sheets first.
const dash=add("融合训练仪表板"), soil=add("土壤候选指标"), detail=add("土壤测试明细"), et0=add("ET0探索实验"), ci=add("置信区间与分组"), audit=add("数据训练与限制");

// Soil detail with formula-backed errors.
title(detail,"SoilLSTM｜最终24小时盲测逐点明细","L");
detail.getRange("A3:L3").values=[["候选","预测时点","目标时点","时域(min)","实测原始读数%","预测%","持久性基线%","变化事件","误差","绝对误差","平方误差","标签口径"]];header(detail,"A3:L3");
const dr=data.soil_predictions.map(r=>[names[r.candidate],r.prediction_time,r.target,r.horizon_min,r.actual,r.predicted,r.baseline,r.event,null,null,null,"未标定探头原始百分比"]);
const dn=dr.length+3;detail.getRange(`A4:L${dn}`).values=dr;detail.getRange("I4").formulas=[["=F4-E4"]];detail.getRange(`I4:I${dn}`).fillDown();detail.getRange("J4").formulas=[["=ABS(I4)"]];detail.getRange(`J4:J${dn}`).fillDown();detail.getRange("K4").formulas=[["=I4^2"]];detail.getRange(`K4:K${dn}`).fillDown();detail.getRange(`B4:C${dn}`).format.numberFormat="yyyy-mm-dd hh:mm";detail.getRange(`E4:K${dn}`).format.numberFormat="0.000";borders(detail,`A3:L${dn}`);detail.freezePanes.freezeRows(3);detail.getRange("A:L").format.columnWidth=15;detail.getRange("B:C").format.columnWidth=20;detail.getRange("A:A").format.columnWidth=24;detail.getRange("L:L").format.columnWidth=25;

// Soil metrics.
title(soil,"SoilLSTM｜四种训练策略与持久性基线对比","O");
soil.getRange("A3:O3").values=[["候选","时域(min)","样本量","MAE","RMSE","R²","偏差","最大绝对误差","±0.5命中","±1.0命中","±2.0命中","基线MAE","较基线提升","稳定区MAE","变化事件MAE"]];header(soil,"A3:O3");
const sr=[];for(const n of soilOrder)for(const h of horizons){const m=data.soil_metrics[n][String(h)];sr.push([names[n],h,m.n,m.mae,m.rmse,m.r2,m.bias,m.max_abs_error,m["within_0.5"],m["within_1.0"],m["within_2.0"],m.baseline.mae,m.mae_improvement_vs_baseline,m.stable_mae,m.event_mae]);}
soil.getRange("A4:O19").values=sr;soil.getRange("D4:H19").format.numberFormat="0.0000";soil.getRange("I4:K19").format.numberFormat="0.0%";soil.getRange("L4:O19").format.numberFormat="0.0000";borders(soil,"A3:O19");soil.freezePanes.freezeRows(3);soil.getRange("A:A").format.columnWidth=27;soil.getRange("B:O").format.columnWidth=14;
section(soil,"A22:O22","可视化辅助数据");soil.getRange("A23:F23").values=[["时域","现有代理","仅现场","1:1混合","迁移学习","持久性基线"]];header(soil,"A23:F23");
for(let i=0;i<4;i++){const h=horizons[i],row=24+i;soil.getRange(`A${row}:F${row}`).values=[[h,...soilOrder.map(n=>data.soil_metrics[n][String(h)].mae),data.soil_metrics.mixed[String(h)].baseline.mae]];}
chart(soil,"line","A23:F27",["A30","H47"],"各时域 MAE 对比（湿度百分点）","0.00");
soil.getRange("H23:K23").values=[["候选","30min MAE","60min MAE","30/60门槛"]];header(soil,"H23:K23");
for(let i=0;i<4;i++){const n=soilOrder[i],r=24+i;soil.getRange(`H${r}:K${r}`).values=[[names[n],data.soil_metrics[n]["30"].mae,data.soil_metrics[n]["60"].mae,n==="mixed"?"通过":"—"]];}
chart(soil,"bar","H23:J27",["I30","O47"],"重点时域：30/60分钟 MAE","0.00");

// ET0 detail and metrics.
title(et0,"N-BEATS ET₀｜虹桥融合训练探索性验证","M");
et0.getRange("A3:M3").values=[["候选","样本量","MAE","RMSE","R²","偏差","最大绝对误差","±0.02命中","±0.05命中","±0.10命中","基线MAE","MAE 95%CI低","MAE 95%CI高"]];header(et0,"A3:M3");
const etNames=[["existing","现有N-BEATS"],["hongqiao_retrain","虹桥重训"],["transfer","虹桥预训练+现场微调"]];
et0.getRange("A4:M6").values=etNames.map(([k,label])=>{const m=data.et0_metrics[k];return[label,m.n,m.mae,m.rmse,m.r2,m.bias,m.max_abs_error,m["within_0.02"],m["within_0.05"],m["within_0.1"],m.baseline.mae,m.bootstrap95.mae_low,m.bootstrap95.mae_high]});et0.getRange("C4:G6").format.numberFormat="0.0000";et0.getRange("H4:J6").format.numberFormat="0.0%";et0.getRange("K4:M6").format.numberFormat="0.0000";borders(et0,"A3:M6");et0.getRange("O3:Q3").values=[["候选","MAE","RMSE"]];for(let i=0;i<3;i++){const [k,label]=etNames[i],m=data.et0_metrics[k];et0.getRange(`O${4+i}:Q${4+i}`).values=[[label,m.mae,m.rmse]];}et0.getRange("O:O").format.columnWidth=28;et0.getRange("P:Q").format.columnWidth=14;
section(et0,"A9:M9","最终24个完整小时逐点结果");et0.getRange("A10:I10").values=[["候选","预测时点","目标小时","实测计算ET₀","预测ET₀","持久性基线","误差","绝对误差","平方误差"]];header(et0,"A10:I10");
const er=data.et0_predictions.map(r=>[etNames.find(x=>x[0]===r.candidate)?.[1]??r.candidate,r.prediction_time,r.target,r.actual,r.predicted,r.baseline,null,null,null]);const en=er.length+10;et0.getRange(`A11:I${en}`).values=er;et0.getRange("G11").formulas=[["=E11-D11"]];et0.getRange(`G11:G${en}`).fillDown();et0.getRange("H11").formulas=[["=ABS(G11)"]];et0.getRange(`H11:H${en}`).fillDown();et0.getRange("I11").formulas=[["=G11^2"]];et0.getRange(`I11:I${en}`).fillDown();et0.getRange(`B11:C${en}`).format.numberFormat="yyyy-mm-dd hh:mm";et0.getRange(`D11:I${en}`).format.numberFormat="0.0000";borders(et0,`A10:I${en}`);et0.getRange("A:A").format.columnWidth=27;et0.getRange("B:C").format.columnWidth=20;et0.getRange("D:M").format.columnWidth=15;chart(et0,"bar","O3:Q6",["J10","Q26"],"ET₀ 候选 MAE/RMSE（mm/h）","0.000");

// CI and stable/event.
title(ci,"6小时分块自助法 95% 置信区间与变化事件表现","N");ci.getRange("A3:H3").values=[["候选","时域(min)","MAE","95%CI低","95%CI高","分块数","稳定区MAE","变化事件MAE"]];header(ci,"A3:H3");
const cr=[];for(const n of soilOrder)for(const h of horizons){const m=data.soil_metrics[n][String(h)];cr.push([names[n],h,m.mae,m.bootstrap95.mae_low,m.bootstrap95.mae_high,m.bootstrap95.blocks,m.stable_mae,m.event_mae]);}ci.getRange("A4:H19").values=cr;ci.getRange("C4:E19").format.numberFormat="0.0000";ci.getRange("G4:H19").format.numberFormat="0.0000";borders(ci,"A3:H19");ci.getRange("A:A").format.columnWidth=27;ci.getRange("B:H").format.columnWidth=15;ci.getRange("J3:L3").values=[["候选/时域","稳定区MAE","变化事件MAE"]];header(ci,"J3:L3");let rr=4;for(const n of soilOrder){for(const h of [30,60]){const m=data.soil_metrics[n][String(h)];ci.getRange(`J${rr}:L${rr}`).values=[[`${names[n]} ${h}min`,m.stable_mae,m.event_mae]];rr++;}}ci.getRange("J:J").format.columnWidth=31;ci.getRange("K:L").format.columnWidth=16;chart(ci,"bar","J3:L11",["J14","Q31"],"平稳区间与变化事件 MAE","0.00");ci.getRange("A22:H25").merge();ci.getRange("A22").values=[["说明：最终测试仅覆盖24小时，共4个六小时块，因此95%区间较宽。变化事件定义为未来12点相对最后实测值最大变化达到0.5个百分点；报告保留自然事件比例，不重采样测试集。"]];ci.getRange("A22:H25").format={fill:"#FFF7ED",font:{color:"#9A3412"},wrapText:true,verticalAlignment:"center"};

// Audit/data limitations.
title(audit,"数据来源、时间切分、训练口径与限制","J");section(audit,"A3:J3","数据分布差异");audit.getRange("A4:F4").values=[["数据源","记录数","时间范围","湿度最小%","中位数%","最大%"]];header(audit,"A4:F4");const fp=data.data_profile.field,hp=data.data_profile.hongqiao;audit.getRange("A5:F6").values=[["现场未标定探头",fp.rows_5min,`${fp.start} 至 ${fp.end}`,fp.soil_min,fp.soil_median,fp.soil_max],["虹桥水量平衡代理",hp.rows_hourly,`${hp.start} 至 ${hp.end}`,hp.soil_min,hp.soil_median,hp.soil_max]];borders(audit,"A4:F6");audit.getRange("D5:F6").format.numberFormat="0.0";
section(audit,"A9:J9","严格时间切分与样本");audit.getRange("A10:B17").values=[["现场训练目标","测试前48小时以前"],["现场验证目标",`${data.field_split.validation_start} 至 ${data.field_split.test_start}`],["现场测试目标",`${data.field_split.test_start} 至 ${data.field_split.end}`],["土壤窗口","此前288个五分钟点预测未来12点"],["训练/验证/测试窗口","957 / 277 / 278"],["标准化","虹桥与现场训练行等量采样拟合"],["训练事件比例","约40%；验证/测试保持自然分布"],["固定种子",String(data.seed)]];header(audit,"A10:A17");borders(audit,"A10:B17");
section(audit,"A20:J20","结论与使用边界");audit.getRange("A21:J28").merge();audit.getRange("A21").values=[["1）现场土壤湿度仅代表未标定探头原始百分比，不宣称为体积含水率。\n2）虹桥湿度为水量平衡代理标签，不是实测土壤含水率。\n3）1:1混合候选在30/60分钟MAE上超过现有代理模型，但5/15分钟仍略逊于持久性基线；60分钟R²仍接近0且置信区间宽。\n4）ET₀迁移候选在24小时探索测试中通过预设门槛，但仅比持久性基线小约0.0005 mm/h，证据很弱。\n5）所有权重均为专家材料候选，不覆盖生产模型、不导出ESP32；现阶段不建议直接替换生产模型。"]];audit.getRange("A21:J28").format={fill:"#FEF2F2",font:{color:red,bold:true},wrapText:true,verticalAlignment:"center"};audit.getRange("H30:K30").values=[["数据源","最小值","中位数","最大值"]];audit.getRange("H31:K32").values=[["现场未标定探头",fp.soil_min,fp.soil_median,fp.soil_max],["虹桥水量平衡代理",hp.soil_min,hp.soil_median,hp.soil_max]];header(audit,"H30:K30");audit.getRange("H:H").format.columnWidth=26;chart(audit,"bar","H30:K32",["A30","J46"],"代理湿度与现场原始读数分布差异（%）","0.0");audit.getRange("A:A").format.columnWidth=24;audit.getRange("B:B").format.columnWidth=62;audit.getRange("C:C").format.columnWidth=54;audit.getRange("D:J").format.columnWidth=16;

// Dashboard.
title(dash,"虹桥代理数据 + 现场实测｜融合训练专家评估","P");dash.getRange("A2:P2").merge();dash.getRange("A2").values=[["候选模型评估 · 生产权重未改动 · 土壤最终24小时盲测 + ET₀探索性验证"]];dash.getRange("A2:P2").format={fill:"#DCEAF4",font:{color:navy,italic:true},horizontalAlignment:"center"};
const mix=data.soil_metrics.mixed,old=data.soil_metrics.existing_proxy,emt=data.et0_metrics.transfer;
const cards=[["A4:D4","土壤推荐候选","1:1 平衡混合",green],["F4:I4","30min MAE",`${fmt4(mix["30"].mae)} 百分点`,blue],["K4:N4","60min MAE",`${fmt4(mix["60"].mae)} 百分点`,blue],["A7:D7","30min 较现有改善",`${(100*(1-mix["30"].mae/old["30"].mae)).toFixed(1)}%`,teal],["F7:I7","60min 较现有改善",`${(100*(1-mix["60"].mae/old["60"].mae)).toFixed(1)}%`,teal],["K7:N7","ET₀迁移 MAE",`${fmt4(emt.mae)} mm/h`,amber]];
for(const [r,l,v,c] of cards){dash.getRange(r).merge();const a=r.split(":")[0];dash.getRange(a).values=[[`${l}\n${v}`]];dash.getRange(r).format={fill:c,font:{bold:true,color:"#FFFFFF",size:13},wrapText:true,verticalAlignment:"center",horizontalAlignment:"center"};dash.getRange(a).format.rowHeight=42;}
dash.getRange("A10:P13").merge();dash.getRange("A10").values=[["阶段性结论：融合训练显著修复了现有代理模型的现场域偏移。1:1混合候选在30/60分钟MAE上分别降至0.4677/0.6100个百分点，达到预设候选门槛；但5/15分钟未超过持久性基线，60分钟R²仍为负，且仅4个六小时测试块。建议作为专家阶段候选，不直接替换生产模型。"]];dash.getRange("A10:P13").format={fill:"#FFF7ED",font:{color:"#7C2D12",bold:true},wrapText:true,verticalAlignment:"center"};
dash.getRange("A16:F16").values=[["时域","现有代理","仅现场","1:1混合","迁移学习","持久性基线"]];header(dash,"A16:F16");for(let i=0;i<4;i++){const h=horizons[i],r=17+i;dash.getRange(`A${r}:F${r}`).values=[[h,...soilOrder.map(n=>data.soil_metrics[n][String(h)].mae),mix[String(h)].baseline.mae]];}chart(dash,"line","A16:F20",["A23","H40"],"SoilLSTM：不同训练策略 MAE","0.00");
dash.getRange("I16:L16").values=[["ET₀候选","MAE","RMSE","基线MAE"]];header(dash,"I16:L16");for(let i=0;i<3;i++){const [k,label]=etNames[i],m=data.et0_metrics[k],r=17+i;dash.getRange(`I${r}:L${r}`).values=[[label,m.mae,m.rmse,m.baseline.mae]];}chart(dash,"bar","I16:L19",["I23","P40"],"ET₀：24小时探索性误差","0.000");
dash.getRange("R2:V2").values=[["目标时点","实测","现有代理","1:1混合","持久性基线"]];const pOld=data.soil_predictions.filter(r=>r.candidate==="existing_proxy"&&r.horizon_min===60);const pMix=data.soil_predictions.filter(r=>r.candidate==="mixed"&&r.horizon_min===60);for(let i=0;i<pMix.length;i++){dash.getRange(`R${3+i}:V${3+i}`).values=[[pMix[i].target,pMix[i].actual,pOld[i].predicted,pMix[i].predicted,pMix[i].baseline]];}chart(dash,"line",`R2:V${2+pMix.length}`,["A43","P62"],"最终24小时：60分钟实测与候选预测","0.0");
dash.getRange("A65:P68").merge();dash.getRange("A65").values=[["答辩建议表述：我们采用虹桥长期代理数据预训练/混合学习一般变化规律，再用现场真实探头读数校正域偏移。候选在30与60分钟均优于现有模型，但对短时平稳变化仍未超过简单基线，因此结果定位为阶段性泛化验证，而非生产替换依据。"]];dash.getRange("A65:P68").format={fill:"#ECFDF5",font:{color:"#166534",bold:true},wrapText:true,verticalAlignment:"center"};dash.getRange("A:P").format.columnWidth=12;dash.freezePanes.freezeRows(2);

const out=await SpreadsheetFile.exportXlsx(wb);await out.save(`${baseDir}双模型现场精度评估_融合训练版.xlsx`);
await fs.mkdir(`${baseDir}work`,{recursive:true});
for(const [sheetName,file,range] of [["融合训练仪表板","01_融合训练仪表板.png","A1:P68"],["土壤候选指标","02_SoilLSTM候选对比.png","A1:O47"],["ET0探索实验","03_ET0融合探索.png","A1:Q26"],["置信区间与分组","04_置信区间与事件表现.png","A1:Q31"],["数据训练与限制","05_数据分布与限制.png","A1:J46"]]){const blob=await wb.render({sheetName,range,scale:1.5,format:"png"});await fs.writeFile(`${baseDir}${file}`,new Uint8Array(await blob.arrayBuffer()));}
for(const [sheetName,range] of [["融合训练仪表板","A1:P68"],["土壤候选指标","A1:O47"],["土壤测试明细","A1:L18"],["ET0探索实验","A1:Q26"],["置信区间与分组","A1:Q31"],["数据训练与限制","A1:J46"]]){const blob=await wb.render({sheetName,range,scale:1,format:"png"});await fs.writeFile(`${baseDir}work/qa_${sheetName}.png`,new Uint8Array(await blob.arrayBuffer()));}
const check=await wb.inspect({kind:"table",range:"融合训练仪表板!A1:P20",include:"values,formulas",tableMaxRows:20,tableMaxCols:16,maxChars:8000});await fs.writeFile(`${baseDir}work/key_inspection.ndjson`,check.ndjson,"utf8");
const errors=await wb.inspect({kind:"match",searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",options:{useRegex:true,maxResults:300},summary:"final formula error scan"});await fs.writeFile(`${baseDir}work/formula_errors.ndjson`,errors.ndjson,"utf8");
console.log(JSON.stringify({xlsx:`${baseDir}双模型现场精度评估_融合训练版.xlsx`,soilRows:dr.length,et0Rows:er.length,pngs:5}));
