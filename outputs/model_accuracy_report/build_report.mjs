import fs from "node:fs/promises";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const baseDir = new URL(".", import.meta.url).pathname;
const rawJson = await fs.readFile(`${baseDir}work/analysis.json`, "utf8");
const data = JSON.parse(rawJson.replace(/\bNaN\b/g, "null"));
const et0Names=["当前部署版","净辐射修正版","持久性基线"];
const wb = Workbook.create();
const navy = "#12304A", teal = "#0E7490", blue = "#2563EB", green = "#15803D";
const amber = "#D97706", red = "#B91C1C", pale = "#EAF2F8", gray = "#64748B";
addSheet("专家仪表板");

function addSheet(name) {
  const s = wb.worksheets.add(name);
  s.showGridLines = false;
  return s;
}
function title(s, text, endCol="L") {
  s.getRange(`A1:${endCol}1`).merge();
  s.getRange("A1").values = [[text]];
  s.getRange(`A1:${endCol}1`).format = {fill: navy, font:{bold:true,color:"#FFFFFF",size:18}, verticalAlignment:"center"};
  s.getRange("A1").format.rowHeight = 34;
}
function section(s, range, text) {
  s.getRange(range).merge();
  const cell = range.split(":")[0];
  s.getRange(cell).values = [[text]];
  s.getRange(range).format = {fill: teal, font:{bold:true,color:"#FFFFFF",size:12}, verticalAlignment:"center"};
}
function header(s, range) {
  s.getRange(range).format = {fill: pale, font:{bold:true,color:navy}, borders:{preset:"inside",style:"thin",color:"#CBD5E1"}, wrapText:true, verticalAlignment:"center"};
}
function borders(s, range) { s.getRange(range).format.borders = {preset:"inside",style:"thin",color:"#E2E8F0"}; }
function valuesMatrix(rows, fields) { return rows.map(r => fields.map(f => r[f] ?? null)); }
function chart(s, type, source, position, chartTitle, yFormat="0.00") {
  const c = s.charts.add(type, s.getRange(source));
  c.title = chartTitle; c.titleTextStyle.fontSize = 12; c.hasLegend = true;
  c.xAxis = {axisType:"textAxis", textStyle:{fontSize:9}};
  c.yAxis = {numberFormatCode:yFormat};
  c.setPosition(...position);
  return c;
}

// 1. 指标说明
{
  const s=addSheet("指标说明"); title(s,"双模型现场精度评估｜指标与口径说明","J");
  section(s,"A3:J3","评估口径");
  const rows=[
    ["ET₀ 对照值","实测计算 ET₀：由现场温度、湿度、风速、气压和双辐射探头净短波辐射，按 FAO-56 Penman–Monteith 小时公式计算。属于参考 ET₀，不是蒸渗仪直接测量值。"],
    ["当前部署版","复现现有在线链路：先得到净短波辐射，再由 FAO-56 默认反照率路径再次乘 0.77，随后输入现有 N-BEATS 权重。"],
    ["净辐射修正版","历史 ET₀ 显式使用双探头净短波辐射，避免再次乘 0.77；沿用同一权重，仅用于诊断，不代表已部署版本。"],
    ["土壤湿度回测","每个预测起点仅使用此前连续 288 个五分钟点，预测未来 5/15/30/60 分钟，与随后实测值比较。"],
    ["持久性基线","假设未来值保持为最后一个已知观测值。平稳序列中该基线很强，必须与模型并列展示。"],
    ["MAE / RMSE","平均绝对误差 / 均方根误差，越小越好；RMSE 对大误差更敏感。"],
    ["R²","决定系数，1 为理想；小于 0 表示不如直接使用目标均值。不能脱离样本分布单独解读。"],
    ["容差命中率","误差落在给定容差内的样本比例，便于专家理解，但不笼统称为“准确率”。"],
  ];
  s.getRange(`A4:B${3+rows.length}`).values=rows; header(s,"A4:A11"); borders(s,"A4:B11");
  section(s,"A13:J13","防止数据泄漏与异常处理");
  s.getRange("A14:B19").values=[
    ["时间重建","依据 bootSessionId 与 uptimeMs 重建北京时间；设备重启时使用新会话 uptimeMs 衔接。"],
    ["ET₀ 完整小时","只保留每个自然小时至少 11 个有效五分钟包的小时，并剔除首尾不完整小时。"],
    ["ET₀ 窗口","使用目标小时前连续 24 个完整小时预测目标小时。"],
    ["土壤窗口","只允许最多 3 个五分钟短缺口做内部线性插值；目标点也必须有效。"],
    ["异常排除","排除完整性或传感器 Ok 标志无效、气压非正、土壤湿度为零的记录。"],
    ["数据来源","7月30日数据用于正式回测；7月29日数据因完整连续窗口不足，仅用于质量和预热覆盖说明。"],
  ]; header(s,"A14:A19"); borders(s,"A14:B19");
  s.getRange("A:B").format.wrapText=true; s.getRange("A:A").format.columnWidth=22; s.getRange("B:B").format.columnWidth=92;
}

// 2. ET0现场回测
{
  const s=addSheet("ET0现场回测"); title(s,"N-BEATS｜未来 1 小时 ET₀ 现场回测","N");
  const heads=["数据源","预测起点","目标小时","实测计算ET₀","当前部署预测","净辐射修正预测","持久性基线","当前误差","当前绝对误差","当前平方误差","修正误差","修正绝对误差","基线误差","模型版本"];
  s.getRange("A3:N3").values=[heads]; header(s,"A3:N3");
  const fields=["source","forecast_origin","target_hour","actual_et0","predicted_deployment","predicted_corrected","baseline_et0",null,null,null,null,null,null,"model_version"];
  const rows=data.et0_rows.map(r=>fields.map(f=>f?r[f]:null)); const n=rows.length+3;
  s.getRange(`A4:N${n}`).values=rows;
  s.getRange("H4").formulas=[["=E4-D4"]]; s.getRange(`H4:H${n}`).fillDown();
  s.getRange("I4").formulas=[["=ABS(H4)"]]; s.getRange(`I4:I${n}`).fillDown();
  s.getRange("J4").formulas=[["=H4^2"]]; s.getRange(`J4:J${n}`).fillDown();
  s.getRange("K4").formulas=[["=F4-D4"]]; s.getRange(`K4:K${n}`).fillDown();
  s.getRange("L4").formulas=[["=ABS(K4)"]]; s.getRange(`L4:L${n}`).fillDown();
  s.getRange("M4").formulas=[["=G4-D4"]]; s.getRange(`M4:M${n}`).fillDown();
  s.getRange(`B4:C${n}`).format.numberFormat="yyyy-mm-dd hh:mm"; s.getRange(`D4:M${n}`).format.numberFormat="0.000";
  borders(s,`A3:N${n}`); s.freezePanes.freezeRows(3); s.getRange("A:N").format.columnWidth=15; s.getRange("B:C").format.columnWidth=20; s.getRange("N:N").format.columnWidth=20;
}

// 3. ET0公式明细
{
  const s=addSheet("ET0公式明细"); title(s,"实测计算 ET₀｜小时聚合与公式输入","M");
  const heads=["小时","数据源","气温°C","相对湿度%","风速m/s","入射辐射W/m²","净短波W/m²","样本数","气压kPa","实测计算ET₀","当前部署输入ET₀","修正输入ET₀","公式说明"];
  s.getRange("A3:M3").values=[heads]; header(s,"A3:M3");
  const fields=["hour","source","air_temperature_c","air_humidity_percent","wind_speed_ms","solar_incoming_wm2","net_shortwave_wm2","sample_count","pressure_kpa","et0_reference","et0_deployment_input","et0_corrected_input"];
  const rows=data.hourly_rows.map(r=>[...fields.map(f=>r[f]??null),"FAO-56 Penman–Monteith（小时）"]); const n=rows.length+3;
  s.getRange(`A4:M${n}`).values=rows; s.getRange(`A4:A${n}`).format.numberFormat="yyyy-mm-dd hh:mm"; s.getRange(`C4:L${n}`).format.numberFormat="0.000";
  borders(s,`A3:M${n}`); s.freezePanes.freezeRows(3); s.getRange("A:M").format.columnWidth=16; s.getRange("A:B").format.columnWidth=21; s.getRange("M:M").format.columnWidth=32;
}

// 4. 土壤湿度回测
{
  const s=addSheet("土壤湿度回测"); title(s,"SoilLSTM｜未来 5–60 分钟土壤湿度现场回测","M");
  const heads=["数据源","预测起点","目标时间","时域(min)","实测湿度%","模型预测%","持久性基线%","模型误差","模型绝对误差","模型平方误差","基线误差","基线绝对误差","模型版本"];
  s.getRange("A3:M3").values=[heads]; header(s,"A3:M3");
  const fields=["source","forecast_origin","target_time","horizon_min","actual_soil","predicted_soil","baseline_soil",null,null,null,null,null,"model_version"];
  const rows=data.soil_rows.map(r=>fields.map(f=>f?r[f]:null)); const n=rows.length+3;
  s.getRange(`A4:M${n}`).values=rows;
  s.getRange("H4").formulas=[["=F4-E4"]]; s.getRange(`H4:H${n}`).fillDown();
  s.getRange("I4").formulas=[["=ABS(H4)"]]; s.getRange(`I4:I${n}`).fillDown();
  s.getRange("J4").formulas=[["=H4^2"]]; s.getRange(`J4:J${n}`).fillDown();
  s.getRange("K4").formulas=[["=G4-E4"]]; s.getRange(`K4:K${n}`).fillDown();
  s.getRange("L4").formulas=[["=ABS(K4)"]]; s.getRange(`L4:L${n}`).fillDown();
  s.getRange(`B4:C${n}`).format.numberFormat="yyyy-mm-dd hh:mm"; s.getRange(`E4:L${n}`).format.numberFormat="0.000";
  borders(s,`A3:M${n}`); s.freezePanes.freezeRows(3); s.getRange("A:M").format.columnWidth=15; s.getRange("B:C").format.columnWidth=20; s.getRange("M:M").format.columnWidth=20;
}

// 5. 离线测试指标
{
  const s=addSheet("离线测试指标"); title(s,"离线测试集指标｜模型训练产物记录","I");
  s.getRange("A3:I3").values=[["模型","MAE","RMSE","R²","基线MAE","基线RMSE","是否可用","数据阶段","说明"]]; header(s,"A3:I3");
  const e=data.offline_metrics.et0, so=data.offline_metrics.soil;
  s.getRange("A4:I5").values=[
    ["N-BEATS ET₀",e.mae,e.rmse,e.r2,e.baseline_mae,e.baseline_rmse,e.usable,"离线测试集","训练产物 metadata.json；不可替代现场验证"],
    ["SoilLSTM",so.mae,so.rmse,so.r2,so.baseline_mae,so.baseline_rmse,so.usable,"离线测试集","训练数据类型为 proxy；不可替代现场验证"],
  ]; s.getRange("B4:F5").format.numberFormat="0.0000"; borders(s,"A3:I5"); s.getRange("A:I").format.columnWidth=18; s.getRange("I:I").format.columnWidth=44;
}

// 6. 数据质量
{
  const s=addSheet("数据质量"); title(s,"现场数据质量与有效回测覆盖","J");
  s.getRange("A3:J3").values=[["数据源","原始记录","起始时间","结束时间","设备会话","土壤零值","已标异常","完整自然小时","ET₀回测点","土壤回测起点"]]; header(s,"A3:J3");
  s.getRange("A4:J5").values=data.quality.map(q=>[q.source,q.records,q.start,q.end,q.boot_sessions,q.zero_soil_rows,q.marked_anomalies,q.complete_natural_hours,q.et0_backtest_points,q.soil_backtest_origins]);
  borders(s,"A3:J5"); s.getRange("A:J").format.columnWidth=17; s.getRange("C:D").format.columnWidth=23;
  s.getRange("A8:J10").merge(); s.getRange("A8").values=[["说明：7月29日数据不足以形成连续 24 小时 ET₀ 历史窗口或 288 点土壤湿度历史窗口，因此不强行计算模型精度；它仍保留在原始数据与质量统计中。"]];
  s.getRange("A8:J10").format={fill:"#FFF7ED",font:{color:"#9A3412"},wrapText:true,verticalAlignment:"center"};
}

// 7. 计算明细（指标汇总）
{
  const s=addSheet("计算明细"); title(s,"现场回测指标汇总与计算规则","N");
  section(s,"A3:N3","ET₀ 现场指标");
  s.getRange("A4:K4").values=[["算法","样本量","MAE","RMSE","R²","平均偏差","最大绝对误差","±0.02命中","±0.05命中","±0.10命中","MAE较基线提升"]]; header(s,"A4:K4");
  s.getRange("A5:K7").values=et0Names.map(name=>{const m=data.et0_metrics[name];return [name,m.n,m.mae,m.rmse,m.r2,m.bias,m.max_abs_error,m["within_0.02"],m["within_0.05"],m["within_0.1"],m.mae_improvement_vs_baseline??null]});
  s.getRange("C5:G7").format.numberFormat="0.0000"; s.getRange("H5:K7").format.numberFormat="0.0%"; borders(s,"A4:K7");
  section(s,"A10:N10","土壤湿度现场指标");
  s.getRange("A11:N11").values=[["时域(min)","对象","样本量","MAE","RMSE","R²","平均偏差","最大绝对误差","±0.5命中","±1.0命中","±2.0命中","MAE较基线提升","结论","误差单位"]]; header(s,"A11:N11");
  const out=[]; for(const h of [5,15,30,60]) for(const obj of ["model","baseline"]){const m=data.soil_metrics[String(h)][obj];out.push([h,obj==="model"?"SoilLSTM":"持久性基线",m.n,m.mae,m.rmse,m.r2,m.bias,m.max_abs_error,m["within_0.5"],m["within_1.0"],m["within_2.0"],m.mae_improvement_vs_baseline??null,obj==="model"?(m.mae_improvement_vs_baseline>0?"优于基线":"现场泛化与校准空间"):"对照基准","土壤湿度百分点"])}
  s.getRange("A12:N19").values=out; s.getRange("D12:H19").format.numberFormat="0.000"; s.getRange("I12:L19").format.numberFormat="0.0%"; borders(s,"A11:N19");
  section(s,"A22:N22","可审计计算规则");
  s.getRange("A23:B28").values=[
    ["误差","预测值 - 实测/参考值；见两张现场回测明细表公式列"],
    ["绝对误差","ABS(误差)"],["平方误差","误差^2"],["MAE","AVERAGE(绝对误差)"],["RMSE","SQRT(AVERAGE(平方误差))"],["提升率","1 - 模型MAE / 持久性基线MAE；负值表示未超过基线"],
  ]; header(s,"A23:A28"); borders(s,"A23:B28"); s.getRange("A:N").format.columnWidth=16; s.getRange("B:B").format.columnWidth=25; s.getRange("M:N").format.columnWidth=23;
}

// 8. 原始数据
{
  const s=addSheet("原始数据"); title(s,"现场原始数据副本｜源文件未修改","X");
  const heads=["数据源","北京时间","实际间隔ms","异常状态","异常说明","source","index","integrityOk","bootSessionId","uptimeMs","windOk","airOk","soilOk","solar1Ok","solar2Ok","气压hPa","风电压","风速m/s","气温°C","湿度%","土温°C","土壤湿度%","反射辐射","入射辐射","净短波辐射"];
  s.getRange("A3:Y3").values=[heads]; header(s,"A3:Y3");
  const fields=["source_file","estimated_time_beijing","actual_interval_ms","anomaly_status","anomaly_note","source","index","integrity_ok","boot_session_id","uptime_ms","wind_ok","air_ok","soil_ok","solar1_ok","solar2_ok","air_pressure_hpa","wind_voltage","wind_speed_ms","air_temperature_c","air_humidity_percent","soil_temperature_c","soil_moisture_percent","solar_reflected_wm2","solar_incoming_wm2","net_shortwave_wm2"];
  const rows=valuesMatrix(data.raw_rows,fields); const n=rows.length+3; s.getRange(`A4:Y${n}`).values=rows; s.getRange(`B4:B${n}`).format.numberFormat="yyyy-mm-dd hh:mm:ss"; borders(s,`A3:Y${n}`); s.freezePanes.freezeRows(3); s.getRange("A:Y").format.columnWidth=13; s.getRange("A:B").format.columnWidth=22; s.getRange("D:E").format.columnWidth=22;
}

// 9. 专家仪表板
{
  const s=wb.worksheets.getItem("专家仪表板"); title(s,"双模型精度量化｜现场泛化验证","P");
  s.getRange("A2:P2").merge(); s.getRange("A2").values=[["现场实测回测 · 严格时间滚动 · 2026-07-30 至 2026-08-05"]]; s.getRange("A2:P2").format={fill:"#DCEAF4",font:{color:navy,italic:true},horizontalAlignment:"center"};
  const cards=[
    ["A4:D4","ET₀ 当前部署 MAE",`${data.et0_metrics["当前部署版"].mae.toFixed(4)} mm/h`,blue],
    ["F4:I4","ET₀ ±0.10 命中率",`${(100*data.et0_metrics["当前部署版"]["within_0.1"]).toFixed(1)}%`,green],
    ["K4:N4","ET₀ 回测样本",String(data.et0_metrics["当前部署版"].n),teal],
    ["A7:D7","土壤 60min MAE",`${data.soil_metrics["60"].model.mae.toFixed(3)} 百分点`,amber],
    ["F7:I7","土壤 60min R²",data.soil_metrics["60"].model.r2.toFixed(3),blue],
    ["K7:N7","土壤预测起点",String(data.soil_metrics["60"].model.n),teal],
  ];
  for(const [r,label,val,color] of cards){s.getRange(r).merge();const c=r.split(":")[0];s.getRange(c).values=[[label+"\n"+val]];s.getRange(r).format={fill:color,font:{bold:true,color:"#FFFFFF",size:13},wrapText:true,verticalAlignment:"center",horizontalAlignment:"center"};s.getRange(c).format.rowHeight=42;}
  s.getRange("A10:P12").merge(); s.getRange("A10").values=[["核心结论：现场 ET₀ 当前部署版 MAE 为 0.0831 mm/h，±0.10 mm/h 命中率 85.9%。单独修正净辐射输入并未改善现有权重，需配套重训练/校准。SoilLSTM 在这段平稳现场数据上未超过持久性基线，应作为现场泛化与再训练依据，而不是选择性隐藏。"]];
  s.getRange("A10:P12").format={fill:"#FFF7ED",font:{color:"#7C2D12",bold:true},wrapText:true,verticalAlignment:"center"};

  // formula-backed chart helpers
  s.getRange("A15:D15").values=[["算法","MAE","RMSE","±0.10命中率"]]; header(s,"A15:D15");
  s.getRange("A16:A18").values=et0Names.map(x=>[x]);
  for(let i=0;i<3;i++){const row=16+i,src=5+i;s.getRange(`B${row}:D${row}`).formulas=[[`='计算明细'!C${src}`,`='计算明细'!D${src}`,`='计算明细'!J${src}`]];}
  s.getRange("F15:H15").values=[["时域(min)","模型MAE","基线MAE"]]; header(s,"F15:H15");
  for(let i=0;i<4;i++){const row=16+i,modelRow=12+i*2,baseRow=modelRow+1;s.getRange(`F${row}:H${row}`).formulas=[[`='计算明细'!A${modelRow}`,`='计算明细'!D${modelRow}`,`='计算明细'!D${baseRow}`]];}
  chart(s,"bar","A15:C18",["A21","H37"],"ET₀：当前版、修正版与基线误差（mm/h）","0.000");
  chart(s,"line","F15:H19",["I21","P37"],"土壤湿度：不同预测时域 MAE（百分点）","0.00");

  // ET0 trend sample helper, linked to detailed sheet.
  s.getRange("R40:U40").values=[["目标小时","实测计算ET₀","当前部署预测","修正版预测"]]; header(s,"R40:U40");
  const displayN=Math.min(72,data.et0_rows.length); for(let i=0;i<displayN;i++){const r=41+i,src=4+i;s.getRange(`R${r}:U${r}`).formulas=[[`='ET0现场回测'!C${src}`,`='ET0现场回测'!D${src}`,`='ET0现场回测'!E${src}`,`='ET0现场回测'!F${src}`]];}
  chart(s,"line",`R40:U${40+displayN}`,["A40","P59"],"ET₀ 代表时段：参考值与两版预测（mm/h）","0.00");
  s.getRange("A61:P63").merge(); s.getRange("A61").values=[["注：ET₀“实测计算值”为传感器观测驱动的 FAO-56 参考值，并非蒸渗仪直接测量。负提升率表示模型未超过持久性基线。离线指标、现场回测和修正诊断必须分开引用。"]]; s.getRange("A61:P63").format={fill:"#F1F5F9",font:{color:gray},wrapText:true,verticalAlignment:"center"};

  // Diagnostic chart helpers outside the print area.
  s.getRange("W2:Z2").values=[["实测计算ET₀","当前部署预测","净辐射修正预测","理想 y=x"]];
  for(let i=0;i<data.et0_rows.length;i++){const r=3+i,src=4+i;s.getRange(`W${r}:Z${r}`).formulas=[[`='ET0现场回测'!D${src}`,`='ET0现场回测'!E${src}`,`='ET0现场回测'!F${src}`,`='ET0现场回测'!D${src}`]];}
  chart(s,"scatter",`W2:Z${2+data.et0_rows.length}`,["A66","H82"],"ET₀：预测值与实测计算值散点（mm/h）","0.00");
  s.getRange("AA2:AC2").values=[["误差区间","当前部署版","修正版"]];
  const bins=[-1,-0.20,-0.10,-0.05,-0.02,0.02,0.05,0.10,0.20,1];
  for(let i=0;i<bins.length-1;i++){
    const r=3+i,lo=bins[i],hi=bins[i+1];s.getRange(`AA${r}`).values=[[`${lo.toFixed(2)}~${hi.toFixed(2)}`]];
    s.getRange(`AB${r}:AC${r}`).formulas=[[`=COUNTIFS('ET0现场回测'!$H$4:$H$131,">="&${lo},'ET0现场回测'!$H$4:$H$131,"<"&${hi})`,`=COUNTIFS('ET0现场回测'!$K$4:$K$131,">="&${lo},'ET0现场回测'!$K$4:$K$131,"<"&${hi})`]];
  }
  chart(s,"bar",`AA2:AC${1+bins.length}`,["I66","P82"],"ET₀：预测误差分布（样本数）","0");
  s.getRange("AE2:AG2").values=[["模型","离线测试R²","现场回测R²"]];
  s.getRange("AE3:AG4").values=[
    ["N-BEATS ET₀",data.offline_metrics.et0.r2,data.et0_metrics["当前部署版"].r2],
    ["SoilLSTM 60min",data.offline_metrics.soil.r2,data.soil_metrics["60"].model.r2],
  ];
  chart(s,"bar","AE2:AG4",["A85","H101"],"离线测试与现场回测 R² 对照","0.00");
  s.getRange("AI2:AL2").values=[["目标时间","实测湿度","SoilLSTM 60min","持久性基线"]];
  const soil60=data.soil_rows.filter(r=>r.horizon_min===60).slice(0,96);
  for(let i=0;i<soil60.length;i++){const r=3+i,src=7+i*4;s.getRange(`AI${r}:AL${r}`).formulas=[[`='土壤湿度回测'!C${src}`,`='土壤湿度回测'!E${src}`,`='土壤湿度回测'!F${src}`,`='土壤湿度回测'!G${src}`]];}
  chart(s,"line",`AI2:AL${2+soil60.length}`,["I85","P101"],"土壤湿度 60min：实测、模型与基线（%）","0.0");
  s.getRange("A103:P105").merge(); s.getRange("A103").values=[["诊断结论：离线测试指标显著高于现场回测，表明当前主要任务是使用真实现场数据重新训练、校准并覆盖灌溉/快速变化事件。净辐射输入修正也应与重新训练配套验证。"]]; s.getRange("A103:P105").format={fill:"#FEF2F2",font:{color:red,bold:true},wrapText:true,verticalAlignment:"center"};
  s.getRange("AN2:AP2").values=[["容差","当前部署版","净辐射修正版"]];
  s.getRange("AN3:AP5").values=[
    ["±0.02 mm/h",data.et0_metrics["当前部署版"]["within_0.02"],data.et0_metrics["净辐射修正版"]["within_0.02"]],
    ["±0.05 mm/h",data.et0_metrics["当前部署版"]["within_0.05"],data.et0_metrics["净辐射修正版"]["within_0.05"]],
    ["±0.10 mm/h",data.et0_metrics["当前部署版"]["within_0.1"],data.et0_metrics["净辐射修正版"]["within_0.1"]],
  ];
  chart(s,"bar","AN2:AP5",["A108","H124"],"ET₀：不同容差下的命中率","0%");
  s.getRange("AR2:AS2").values=[["绝对误差区间","60min样本数"]];
  const soilBins=[[0,0.25],[0.25,0.5],[0.5,1],[1,2],[2,5],[5,20]];
  for(let i=0;i<soilBins.length;i++){const r=3+i,[lo,hi]=soilBins[i];s.getRange(`AR${r}`).values=[[`${lo.toFixed(2)}~${hi.toFixed(2)}`]];s.getRange(`AS${r}`).formulas=[[`=COUNTIFS('土壤湿度回测'!$D$4:$D$6139,60,'土壤湿度回测'!$I$4:$I$6139,">="&${lo},'土壤湿度回测'!$I$4:$I$6139,"<"&${hi})`]];}
  chart(s,"bar","AR2:AS8",["I108","P124"],"SoilLSTM 60min：绝对误差分布（样本数）","0");
  s.getRange("A126:P128").merge(); s.getRange("A126").values=[["建议用于专家答辩的表述：模型已完成真实场景回测，ET₀ 在 ±0.10 mm/h 容差下命中率为 85.9%；同时识别出土壤模型存在现场域偏移，下一步将以真实灌溉事件数据进行再训练与校准。"]]; s.getRange("A126:P128").format={fill:"#ECFDF5",font:{color:"#166534",bold:true},wrapText:true,verticalAlignment:"center"};
  s.getRange("A:P").format.columnWidth=12; s.freezePanes.freezeRows(2);
}

// Put dashboard first by leaving creation order intact but select is not required.
const outputDir=`${baseDir}`;
const out=await SpreadsheetFile.exportXlsx(wb); await out.save(`${outputDir}双模型现场精度评估_专家版.xlsx`);
for(const [sheetName,fileName,range] of [
  ["专家仪表板","01_专家仪表板.png","A1:P63"],
  ["计算明细","02_双模型指标汇总.png","A1:N28"],
  ["数据质量","03_数据质量与覆盖.png","A1:J10"],
  ["专家仪表板","04_模型诊断图表.png","A65:P128"],
]){
  const blob=await wb.render({sheetName,range,scale:1.5,format:"png"});
  await fs.writeFile(`${outputDir}${fileName}`,new Uint8Array(await blob.arrayBuffer()));
}
for(const [sheetName,range] of [
  ["指标说明","A1:B19"],["ET0现场回测","A1:N18"],["ET0公式明细","A1:M18"],
  ["土壤湿度回测","A1:M18"],["离线测试指标","A1:I5"],["原始数据","A1:Y15"]
]){
  const blob=await wb.render({sheetName,range,scale:1,format:"png"});
  await fs.writeFile(`${baseDir}work/qa_${sheetName}.png`,new Uint8Array(await blob.arrayBuffer()));
}
const inspection=await wb.inspect({kind:"workbook,sheet,drawing",maxChars:12000,tableMaxRows:5,tableMaxCols:8});
await fs.writeFile(`${baseDir}work/inspection.ndjson`,inspection.ndjson,"utf8");
const errors=await wb.inspect({kind:"match",searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",options:{useRegex:true,maxResults:300},summary:"final formula error scan"});
await fs.writeFile(`${baseDir}work/formula_errors.ndjson`,errors.ndjson,"utf8");
console.log(JSON.stringify({xlsx:`${outputDir}双模型现场精度评估_专家版.xlsx`,pngs:4,et0:data.et0_rows.length,soil:data.soil_rows.length}));
