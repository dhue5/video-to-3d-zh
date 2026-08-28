import http from 'node:http';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';

const APP_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(APP_DIR, '..');
const DEFAULT_OUTPUT_ROOT = path.join(os.homedir(), 'Downloads');
const DATA_ROOT = path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'VideoTo3DStudio');
const CONFIG_PATH = path.join(DATA_ROOT, 'settings.json');
const PORT = Number(process.env.VIDEO_TO_3D_PORT || 43120);

const QUALITY = {
  FAST: { label: '快速', frames: 8 },
  BALANCED: { label: '均衡', frames: 12 },
  DETAILED: { label: '精细', frames: 20 },
};

const STAGES = [
  ['preparing', '准备项目'],
  ['probing', '分析视频'],
  ['extracting', '抽取关键帧'],
  ['analyzing', 'AI 分析'],
  ['building', '生成模型'],
  ['saving', '保存项目'],
  ['exporting', '输出模型'],
];

const tasks = new Map();
const clients = new Map();

function safeText(value, fallback = '') {
  return typeof value === 'string' ? value.trim() : fallback;
}

function safeName(value) {
  const cleaned = safeText(value, '项目').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/\s+/g, ' ').trim();
  return (cleaned || '项目').slice(0, 100);
}

function v1Root(value) {
  const endpoint = safeText(value).replace(/\/+$/, '');
  const marker = endpoint.toLowerCase().indexOf('/v1');
  return marker >= 0 ? endpoint.slice(0, marker) + '/v1' : endpoint + '/v1';
}

function chatEndpoint(value) {
  return v1Root(value) + '/chat/completions';
}

function modelsEndpoint(value) {
  return v1Root(value) + '/models';
}

function requestHeaders(config, json = false) {
  const headers = {};
  if (json) headers['content-type'] = 'application/json';
  const key = safeText(config.apiKey);
  if (key && config.authType !== 'NONE') {
    if (config.authType === 'X_API_KEY') headers['x-api-key'] = key;
    else headers.authorization = `Bearer ${key}`;
  }
  return headers;
}

function publicConfig(config) {
  return {
    apiUrl: safeText(config.apiUrl),
    modelName: safeText(config.modelName),
    authType: config.authType || 'BEARER',
    rememberKey: Boolean(config.rememberKey),
    hasKey: Boolean(safeText(config.apiKey)),
    blenderPath: safeText(config.blenderPath),
    ffmpegPath: safeText(config.ffmpegPath, 'ffmpeg'),
    ffprobePath: safeText(config.ffprobePath, 'ffprobe'),
    outputRoot: safeText(config.outputRoot, DEFAULT_OUTPUT_ROOT),
    quality: config.quality || 'BALANCED',
    outputFormat: config.outputFormat || 'glb',
  };
}

async function readConfig() {
  try {
    const raw = await fs.readFile(CONFIG_PATH, 'utf8');
    return JSON.parse(raw);
  } catch {
    return {
      apiUrl: '',
      modelName: '',
      apiKey: '',
      authType: 'BEARER',
      rememberKey: false,
      blenderPath: '',
      ffmpegPath: 'ffmpeg',
      ffprobePath: 'ffprobe',
      outputRoot: DEFAULT_OUTPUT_ROOT,
      quality: 'BALANCED',
      outputFormat: 'glb',
    };
  }
}

async function saveConfig(input) {
  await fs.mkdir(DATA_ROOT, { recursive: true });
  const previous = await readConfig();
  const next = {
    ...previous,
    apiUrl: safeText(input.apiUrl),
    modelName: safeText(input.modelName),
    authType: ['BEARER', 'X_API_KEY', 'NONE'].includes(input.authType) ? input.authType : 'BEARER',
    rememberKey: Boolean(input.rememberKey),
    blenderPath: safeText(input.blenderPath),
    ffmpegPath: safeText(input.ffmpegPath, 'ffmpeg'),
    ffprobePath: safeText(input.ffprobePath, 'ffprobe'),
    outputRoot: safeText(input.outputRoot, DEFAULT_OUTPUT_ROOT),
    quality: QUALITY[input.quality] ? input.quality : 'BALANCED',
    outputFormat: ['glb', 'gltf', 'obj', 'fbx'].includes(input.outputFormat) ? input.outputFormat : 'glb',
  };
  if (next.rememberKey) next.apiKey = safeText(input.apiKey) || safeText(previous.apiKey);
  else next.apiKey = '';
  await fs.writeFile(CONFIG_PATH, JSON.stringify(next, null, 2), 'utf8');
  return next;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString('utf8');
  return text ? JSON.parse(text) : {};
}

async function writeJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(body);
}

async function writeFile(response, filePath, contentType) {
  try {
    const data = await fs.readFile(filePath);
    response.writeHead(200, { 'content-type': contentType, 'cache-control': 'no-store' });
    response.end(data);
  } catch {
    response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    response.end('Not found');
  }
}

function emit(projectId, event) {
  const set = clients.get(projectId);
  if (!set) return;
  const data = `data: ${JSON.stringify(event)}\n\n`;
  for (const response of set) response.write(data);
}

async function appendLog(task, message, level = 'info') {
  const line = `[${new Date().toISOString()}] [${level}] ${message}`;
  task.logLines.push(line);
  if (task.logLines.length > 500) task.logLines.shift();
  try {
    await fs.appendFile(task.logPath, line + '\n', 'utf8');
  } catch {
    // The task status is still useful if a log file cannot be written.
  }
  emit(task.id, { type: 'log', line, level });
}

async function writeState(task, patch = {}) {
  task.state = { ...task.state, ...patch, updatedAt: new Date().toISOString() };
  await fs.writeFile(task.statePath, JSON.stringify(task.state, null, 2), 'utf8');
  emit(task.id, { type: 'state', state: task.state });
}

async function setStage(task, stage, percent, message, current = 0, total = 0) {
  task.state.stage = stage;
  task.state.stageLabel = STAGES.find(([key]) => key === stage)?.[1] || stage;
  task.state.percent = Math.max(0, Math.min(100, Math.round(percent)));
  task.state.message = message;
  task.state.current = current;
  task.state.total = total;
  await writeState(task);
  emit(task.id, { type: 'progress', state: task.state });
  await appendLog(task, message);
}

function runProcess(task, command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, cwd: options.cwd || task.projectPath });
    task.children.add(child);
    let stdout = '';
    let stderr = '';
    const consume = (stream, isError) => {
      stream.on('data', (chunk) => {
        const text = chunk.toString();
        if (isError) stderr += text;
        else stdout += text;
        const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        for (const line of lines) {
          if (options.onLine) options.onLine(line, isError);
          else appendLog(task, line, isError ? 'stderr' : 'stdout');
        }
      });
    };
    consume(child.stdout, false);
    consume(child.stderr, true);
    child.on('error', (error) => {
      task.children.delete(child);
      reject(error);
    });
    child.on('close', (code, signal) => {
      task.children.delete(child);
      if (code === 0) resolve({ stdout, stderr, code, signal });
      else reject(new Error(`${path.basename(command)} 退出码 ${code ?? 'null'}${signal ? `（${signal}）` : ''}`));
    });
  });
}

async function probeVideo(task, probePath) {
  const result = await runProcess(task, probePath, [
    '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', task.videoPath,
  ], { onLine: (line) => appendLog(task, line, 'probe') });
  const data = JSON.parse(result.stdout);
  const stream = (data.streams || []).find((item) => item.codec_type === 'video') || {};
  const duration = Number(stream.duration || data.format?.duration || 0);
  const fpsText = String(stream.avg_frame_rate || stream.r_frame_rate || '0/1');
  const [a, b] = fpsText.split('/').map(Number);
  const fps = b ? a / b : Number(fpsText) || 0;
  return {
    duration: Number.isFinite(duration) ? duration : 0,
    fps: Number.isFinite(fps) ? fps : 0,
    width: Number(stream.width || 0),
    height: Number(stream.height || 0),
    frameCount: Math.max(0, Math.round(duration * fps)),
  };
}

async function extractFrames(task, ffmpegPath, probe) {
  const quality = QUALITY[task.options.quality] || QUALITY.BALANCED;
  const count = quality.frames;
  const frameDir = path.join(task.projectPath, 'work', 'frames');
  const pattern = path.join(frameDir, 'frame_%05d.png');
  const duration = Math.max(1, probe.duration || count);
  const rate = (count / duration).toFixed(6);
  await runProcess(task, ffmpegPath, [
    '-y', '-hide_banner', '-loglevel', 'error', '-i', task.videoPath,
    '-vf', `scale=1024:-2,fps=${rate}`, '-frames:v', String(count), pattern,
  ], { onLine: (line) => appendLog(task, line, 'ffmpeg') });
  const names = (await fs.readdir(frameDir)).filter((name) => /^frame_\d+\.png$/i.test(name)).sort();
  if (!names.length) throw new Error('FFmpeg 没有生成关键帧，请检查视频编码和路径。');
  return names.map((name) => path.join(frameDir, name));
}

function modelRecords(payload) {
  const rows = payload?.data || payload?.models || payload?.model_list || payload?.模型 || [];
  const list = Array.isArray(rows) ? rows : Object.values(rows || {});
  const seen = new Map();
  for (const row of list) {
    const id = typeof row === 'string' ? row.trim() : safeText(row?.id || row?.model || row?.name || row?.模型名称);
    if (id) seen.set(id, { id, provider: safeText(row?.owned_by || row?.provider || row?.提供方) });
  }
  return [...seen.values()].sort((a, b) => a.id.localeCompare(b.id));
}

async function fetchModels(config) {
  const response = await fetch(modelsEndpoint(config.apiUrl), { headers: requestHeaders(config) });
  const text = await response.text();
  if (!response.ok) throw new Error(`模型列表请求失败 HTTP ${response.status}`);
  const payload = JSON.parse(text);
  const models = modelRecords(payload);
  if (!models.length) throw new Error('接口返回中没有找到模型列表。');
  return models;
}

function responseText(payload) {
  const value = payload?.choices?.[0]?.message?.content ?? payload?.choices?.[0]?.text ?? payload?.content ?? payload?.text ?? payload?.answer ?? '';
  if (Array.isArray(value)) return value.map((item) => item?.text || item?.content || '').join('');
  return String(value || '');
}

function extractJson(text) {
  const cleaned = String(text || '').replace(/^```(?:json)?/i, '').replace(/```$/i, '').trim();
  try { return JSON.parse(cleaned); } catch {
    const start = cleaned.indexOf('{');
    const end = cleaned.lastIndexOf('}');
    if (start >= 0 && end > start) return JSON.parse(cleaned.slice(start, end + 1));
  }
  throw new Error('AI 返回内容不是可解析的 JSON 建模参数。');
}

function makePrompt(frameCount) {
  return `你是通用三维建模参数分析器。根据 ${frameCount} 张同一物体的不同角度关键帧，输出严格 JSON，禁止 Markdown 和解释文字。使用 3 到 15 个可编辑基础部件，类型只能是 box、cylinder、sphere、torus、cone。尺寸使用相对单位，让整体最大尺寸约为 2。格式必须为：{"object_name":"物体名称","parts":[{"name":"部件名称","type":"box|cylinder|sphere|torus|cone","dimensions":[x,y,z],"position":[x,y,z],"rotation":[rx,ry,rz],"color":[r,g,b]}]}`;
}

async function analyzeWithAI(task, config, framePaths) {
  const content = [{ type: 'text', text: makePrompt(framePaths.length) }];
  for (const framePath of framePaths.slice(0, 6)) {
    const encoded = (await fs.readFile(framePath)).toString('base64');
    content.push({ type: 'image_url', image_url: { url: `data:image/png;base64,${encoded}` } });
  }
  const response = await fetch(chatEndpoint(config.apiUrl), {
    method: 'POST',
    headers: requestHeaders(config, true),
    body: JSON.stringify({ model: safeText(config.modelName), messages: [{ role: 'user', content }], stream: false, max_tokens: 3000 }),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`AI 请求失败 HTTP ${response.status}`);
  const payload = JSON.parse(text);
  return extractJson(responseText(payload));
}

async function uniqueProjectPath(root, base) {
  await fs.mkdir(root, { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, '').replace('T', '-');
  const prefix = `${stamp}_${safeName(base)}`;
  let candidate = path.join(root, prefix);
  let index = 2;
  while (fsSync.existsSync(candidate)) candidate = path.join(root, `${prefix}-${index++}`);
  return candidate;
}

async function createTask(options) {
  const videoPath = path.resolve(safeText(options.videoPath));
  if (!fsSync.existsSync(videoPath)) throw new Error('找不到输入视频文件。请填写完整路径。');
  const outputRoot = path.resolve(safeText(options.outputRoot, DEFAULT_OUTPUT_ROOT));
  const projectPath = await uniqueProjectPath(outputRoot, path.basename(videoPath, path.extname(videoPath)));
  await fs.mkdir(path.join(projectPath, 'source'), { recursive: true });
  await fs.mkdir(path.join(projectPath, 'work', 'frames'), { recursive: true });
  await fs.mkdir(path.join(projectPath, 'logs'), { recursive: true });
  await fs.mkdir(path.join(projectPath, 'outputs'), { recursive: true });
  const id = randomUUID();
  const statePath = path.join(projectPath, 'state.json');
  const logPath = path.join(projectPath, 'logs', 'pipeline.log');
  const task = {
    id, videoPath, projectPath, statePath, logPath, options, children: new Set(), logLines: [],
    state: {
      id, status: 'RUNNING', stage: 'preparing', stageLabel: '准备项目', percent: 0,
      message: '正在创建项目目录', current: 0, total: 0, videoPath, projectPath,
      startedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    },
  };
  tasks.set(id, task);
  await fs.writeFile(path.join(projectPath, 'project.json'), JSON.stringify({
    app: 'video-to-3d-studio', version: '0.3.0', id, videoPath, projectPath,
    quality: options.quality, outputFormat: options.outputFormat, createdAt: task.state.startedAt,
  }, null, 2), 'utf8');
  await writeState(task);
  await appendLog(task, `项目已创建：${projectPath}`);
  return task;
}

async function runBlender(task, blenderPath, specPath) {
  if (!safeText(blenderPath)) throw new Error('请填写 Blender 可执行文件路径。');
  if (!fsSync.existsSync(blenderPath)) throw new Error(`找不到 Blender：${blenderPath}`);
  const scriptPath = path.join(APP_DIR, 'blender_build.py');
  const outputBase = path.join(task.projectPath, 'outputs', 'video_to_3d_model');
  const args = [
    '-b', '--factory-startup', '--python', scriptPath, '--',
    '--spec', specPath, '--blend', `${outputBase}.blend`, '--export', `${outputBase}.${task.options.outputFormat}`,
    '--format', task.options.outputFormat,
  ];
  await runProcess(task, blenderPath, args, {
    onLine: (line) => {
      const match = line.match(/^V3D_PROGRESS\s+(\d+)\s*(.*)$/);
      if (match) {
        const percent = 78 + Math.min(20, Number(match[1]) * 0.2);
        setStage(task, 'building', percent, match[2] || 'Blender 正在生成模型');
      } else appendLog(task, line, 'blender');
    },
  });
  return { blend: `${outputBase}.blend`, model: `${outputBase}.${task.options.outputFormat}` };
}

async function runPipeline(task) {
  const storedConfig = await readConfig();
  const config = { ...storedConfig, ...task.options };
  if (!safeText(task.options.apiKey)) config.apiKey = storedConfig.apiKey || '';
  const ffmpegPath = safeText(task.options.ffmpegPath, config.ffmpegPath || 'ffmpeg');
  const ffprobePath = safeText(task.options.ffprobePath, config.ffprobePath || 'ffprobe');
  try {
    await setStage(task, 'preparing', 3, '项目目录已准备完成');
    await setStage(task, 'probing', 8, '正在读取视频时长、分辨率和帧率');
    const probe = await probeVideo(task, ffprobePath);
    await fs.writeFile(path.join(task.projectPath, 'video_info.json'), JSON.stringify(probe, null, 2), 'utf8');
    await setStage(task, 'extracting', 12, `正在抽取关键帧（${(QUALITY[task.options.quality] || QUALITY.BALANCED).label}）`);
    const frames = await extractFrames(task, ffmpegPath, probe);
    await setStage(task, 'extracting', 35, `关键帧完成：${frames.length} 张`, frames.length, frames.length);
    if (!safeText(config.apiUrl) || !safeText(config.modelName)) throw new Error('请先在软件中填写 AI 接口 URL 和模型名称。');
    if (!safeText(config.apiKey) && config.authType !== 'NONE') throw new Error('请先填写 AI 接口密钥，或选择“不使用密钥”。');
    await setStage(task, 'analyzing', 42, `正在调用 ${config.modelName} 分析关键帧`);
    const spec = await analyzeWithAI(task, config, frames);
    const specPath = path.join(task.projectPath, 'model_spec.json');
    await fs.writeFile(specPath, JSON.stringify(spec, null, 2), 'utf8');
    await setStage(task, 'analyzing', 72, 'AI 分析完成，已保存建模参数');
    await setStage(task, 'building', 78, '正在调用 Blender 生成可编辑模型');
    const output = await runBlender(task, safeText(task.options.blenderPath, config.blenderPath), specPath);
    await setStage(task, 'saving', 96, '正在写入项目结果');
    await writeState(task, { status: 'COMPLETED', percent: 100, stage: 'exporting', stageLabel: '输出模型', message: '建模完成', output, finishedAt: new Date().toISOString() });
    await appendLog(task, `建模完成：${output.model}`);
  } catch (error) {
    const message = task.state.status === 'CANCELLED' ? '任务已取消' : (error?.message || String(error));
    await writeState(task, { status: task.state.status === 'CANCELLED' ? 'CANCELLED' : 'FAILED', message, finishedAt: new Date().toISOString() });
    await appendLog(task, message, 'error');
  } finally {
    for (const child of task.children) child.kill();
    emit(task.id, { type: 'done', state: task.state });
  }
}

async function listProjects(root) {
  const result = [];
  try {
    const entries = await fs.readdir(root, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const projectPath = path.join(root, entry.name);
      try {
        const data = JSON.parse(await fs.readFile(path.join(projectPath, 'state.json'), 'utf8'));
        result.push(data);
      } catch {
        // Ignore unrelated folders in Downloads.
      }
    }
  } catch {
    // The default output folder may not exist yet.
  }
  return result.sort((a, b) => String(b.startedAt || '').localeCompare(String(a.startedAt || ''))).slice(0, 30);
}

async function handle(request, response) {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (request.method === 'GET' && url.pathname === '/') return writeFile(response, path.join(APP_DIR, 'index.html'), 'text/html; charset=utf-8');
  if (request.method === 'GET' && url.pathname === '/api/config') return writeJson(response, 200, publicConfig(await readConfig()));
  if (request.method === 'GET' && url.pathname === '/api/projects') {
    const config = await readConfig();
    return writeJson(response, 200, { projects: await listProjects(config.outputRoot || DEFAULT_OUTPUT_ROOT) });
  }
  if (request.method === 'GET' && url.pathname === '/api/events') {
    const id = url.searchParams.get('project');
    if (!id || !tasks.has(id)) return writeJson(response, 404, { error: '任务不存在或已重启。' });
    response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive' });
    if (!clients.has(id)) clients.set(id, new Set());
    clients.get(id).add(response);
    response.write(`data: ${JSON.stringify({ type: 'state', state: tasks.get(id).state })}\n\n`);
    request.on('close', () => clients.get(id)?.delete(response));
    return;
  }
  if (request.method === 'POST' && url.pathname === '/api/config') {
    const input = await readBody(request);
    const config = await saveConfig(input);
    return writeJson(response, 200, publicConfig(config));
  }
  if (request.method === 'POST' && url.pathname === '/api/models') {
    const input = await readBody(request);
    const config = { ...(await readConfig()), ...input };
    try { return writeJson(response, 200, { models: await fetchModels(config), endpoint: modelsEndpoint(config.apiUrl) }); }
    catch (error) { return writeJson(response, 400, { error: error.message }); }
  }
  if (request.method === 'POST' && url.pathname === '/api/test') {
    const input = await readBody(request);
    const config = { ...(await readConfig()), ...input };
    try {
      const responseData = await fetch(chatEndpoint(config.apiUrl), {
        method: 'POST', headers: requestHeaders(config, true),
        body: JSON.stringify({ model: safeText(config.modelName), messages: [{ role: 'user', content: '只回复：接口测试成功' }], stream: false, max_tokens: 32 }),
      });
      if (!responseData.ok) throw new Error(`接口测试失败 HTTP ${responseData.status}`);
      return writeJson(response, 200, { message: responseText(JSON.parse(await responseData.text())) || '接口测试成功', endpoint: chatEndpoint(config.apiUrl) });
    } catch (error) { return writeJson(response, 400, { error: error.message }); }
  }
  if (request.method === 'POST' && url.pathname === '/api/start') {
    try {
      const options = await readBody(request);
      const task = await createTask(options);
      runPipeline(task);
      return writeJson(response, 200, { id: task.id, projectPath: task.projectPath, state: task.state });
    } catch (error) { return writeJson(response, 400, { error: error.message }); }
  }
  if (request.method === 'POST' && url.pathname === '/api/cancel') {
    const input = await readBody(request);
    const task = tasks.get(input.id);
    if (!task) return writeJson(response, 404, { error: '任务不存在。' });
    task.state.status = 'CANCELLED';
    await writeState(task, { status: 'CANCELLED', message: '正在取消任务…' });
    for (const child of task.children) {
      if (process.platform === 'win32' && child.pid) spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
      else child.kill('SIGTERM');
    }
    return writeJson(response, 200, { message: '已发送取消请求。' });
  }
  if (request.method === 'GET' && url.pathname === '/api/health') return writeJson(response, 200, { ok: true, app: 'video-to-3d-studio', version: '0.3.0' });
  return writeJson(response, 404, { error: 'Not found' });
}

const server = http.createServer((request, response) => {
  handle(request, response).catch((error) => writeJson(response, 500, { error: error.message }));
});

server.listen(PORT, '127.0.0.1', () => {
  const address = `http://127.0.0.1:${PORT}`;
  console.log(`VideoTo3D Studio running at ${address}`);
  if (process.platform === 'win32' && process.env.VIDEO_TO_3D_NO_BROWSER !== '1') {
    spawn('cmd.exe', ['/c', 'start', '', address], { detached: true, windowsHide: true, stdio: 'ignore' }).unref();
  }
});
