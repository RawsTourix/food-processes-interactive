// Instrumented pywebview smoke test. Runs only with --probe, no simulated user clicks.
window.addEventListener("pywebviewready", async () => {
  const started = performance.now();
  const report = {platform: window.pywebview.platform, checks: {}, error: null};
  const api = (op, args) => window.pywebview.api.request({api_version:"0.1",request_id:"probe",op,args});
  try {
    const heat = await api("session.create", {module_id:"heat-exchanger"});
    if (heat.outcome !== "ok") throw new Error("heat session");
    const h = heat.data.session_id;
    const input = await api("session.command", {session_id:h,expected_revision:0,action:"set_conditions",params:{flow:"counter",hot_in:{value:80,unit:"degC"},cold_in:{value:20,unit:"degC"},hot_capacity:{value:1000,unit:"W/K"},cold_capacity:{value:1000,unit:"W/K"},UA:{value:1000,unit:"W/K"}}});
    const snap = await api("session.command", {session_id:h,expected_revision:input.data.revision,action:"capture_snapshot",params:{}});
    const fix = await api("session.command", {session_id:h,expected_revision:snap.data.revision,action:"show_fixture",params:{snapshot_id:snap.data.snapshot.snapshot_id}});
    const real = await api("session.command", {session_id:h,expected_revision:snap.data.revision,action:"get_stand_readings",params:{}});
    report.checks.heat = fix.outcome === "ok" && fix.data.fixture.kind === "demonstration_fixture" && real.diagnostics[0].code === "model.stand_unavailable";
    const previous = await api("storage.read", {target:"probe.json"});
    const air = previous.outcome === "ok" ? await api("session.restore", {document:previous.data.document}) : await api("session.create", {module_id:"humid-air"});
    report.checks.restore_previous = previous.outcome === "ok" ? air.outcome === "ok" && air.data.state.selection.point_id === "p1" : null;
    const a = air.data.session_id;
    const point = await api("session.command", {session_id:a,expected_revision:air.data.revision,action:"select_region",params:{region_id:"schematic-area-a",point_id:"p1"}});
    const properties = await api("session.command", {session_id:a,expected_revision:point.data.revision,action:"get_properties",params:{}});
    const help = await api("material.get", {material_id:"air.region"});
    report.checks.air = point.data.selection.representation === "qualitative" && properties.diagnostics[0].code === "model.psychrometrics_unavailable" && help.outcome === "ok";
    const doc = await api("session.save", {session_id:a,expected_revision:point.data.revision});
    const saved = await api("storage.write", {target:"probe.json",document:doc.data.document,expected_generation:previous.outcome === "ok" ? previous.data.generation : null});
    const read = await api("storage.read", {target:"probe.json"});
    report.checks.storage = saved.outcome === "ok" && saved.data.generation === read.data.generation && read.data.document.module_data.selection.point_id === "p1";
    report.checks.dom = document.documentElement.lang === "ru" && Boolean(document.querySelector("svg#air-svg")) && getComputedStyle(document.getElementById("heat-svg")).display !== "none";
    report.checks.cyrillic = document.getElementById("description").textContent.includes("Условная схема");
    report.checks.svg_viewbox = document.getElementById("heat-svg").getAttribute("viewBox") === "0 0 600 200";
    report.checks.inline_html = location.protocol === "about:";
    report.seconds = Math.round(performance.now() - started) / 1000;
  } catch (error) { report.error = String(error); }
  await window.pywebview.api.report(report);
});
