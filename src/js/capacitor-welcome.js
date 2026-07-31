// main.js
// Cliente WebSocket PCM 16-bit LE stereo 48 kHz para Capacitor/WebView.
// Para USB via ADB, rode no PC:
//   adb reverse tcp:5001 tcp:5001
// Depois conecte em:
//   ws://127.0.0.1:5001

const WS_URL = "ws://127.0.0.1:5001";

const SAMPLE_RATE = 48000;
const CHANNELS = 2;

// 2400 frames = 50 ms em 48 kHz.
// 4800 frames = 100 ms em 48 kHz.
const TARGET_BUFFERED_FRAMES = 2400;
const MAX_BUFFERED_FRAMES = 4800;
const START_BUFFERED_FRAMES = 2400;

let audioContext = null;
let workletNode = null;
let websocket = null;
let started = false;
let resumeTimer = null;
let reconnectTimer = null;
let backgroundKeepAliveEnabled = false;
let userStopped = false;
let reconnectAttempts = 0;
let connectionToken = 0;

const RECONNECT_BASE_DELAY = 1000;
const RECONNECT_MAX_DELAY = 10000;

const workletSource = `
class PCMPlayerProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();

    this.channels = options.processorOptions.channels || 2;
    this.targetBufferedFrames = options.processorOptions.targetBufferedFrames || 960;
    this.maxBufferedFrames = options.processorOptions.maxBufferedFrames || 2400;
    this.startBufferedFrames = options.processorOptions.startBufferedFrames || 960;

    this.queue = [];
    this.readFrameOffset = 0;

    this.underflows = 0;
    this.droppedChunks = 0;
    this.receivedChunks = 0;
    this.processedFrames = 0;
    this.playbackStarted = false;
    this.inUnderflow = false;
    this.smoothedBufferedFrames = 0;

    this.port.onmessage = (event) => {
      if (event.data && event.data.type === "reset") {
        this.queue = [];
        this.readFrameOffset = 0;
        this.playbackStarted = false;
        this.inUnderflow = false;
        this.smoothedBufferedFrames = 0;
        return;
      }

      const arrayBuffer = event.data;
      if (!arrayBuffer || arrayBuffer.byteLength === 0) return;

      const pcm = new Int16Array(arrayBuffer);
      this.queue.push(pcm);
      this.receivedChunks++;

      this.dropOldAudioIfNeeded();
    };
  }

  bufferedFrames() {
    let frames = -this.readFrameOffset;

    for (let i = 0; i < this.queue.length; i++) {
      frames += Math.floor(this.queue[i].length / this.channels);
    }

    return frames;
  }

  dropOldAudioIfNeeded() {
    let frames = this.bufferedFrames();

    if (frames <= this.maxBufferedFrames) return;

    while (this.queue.length > 1 && frames > this.targetBufferedFrames) {
      const dropped = this.queue.shift();
      frames -= Math.floor(dropped.length / this.channels);
      this.readFrameOffset = 0;
      this.droppedChunks++;
    }

    this.port.postMessage({
      type: "stats",
      bufferedFrames: frames,
      droppedChunks: this.droppedChunks,
      underflows: this.underflows,
      receivedChunks: this.receivedChunks
    });
  }

  postStatsIfNeeded(renderedFrames) {
    this.processedFrames += renderedFrames;
    if (this.processedFrames < sampleRate / 2) return;

    this.processedFrames = 0;
    const buffered = this.bufferedFrames();
    this.smoothedBufferedFrames = this.smoothedBufferedFrames === 0
      ? buffered
      : Math.round((this.smoothedBufferedFrames * 0.7) + (buffered * 0.3));

    this.port.postMessage({
      type: "stats",
      bufferedFrames: buffered,
      displayBufferedFrames: this.smoothedBufferedFrames,
      droppedChunks: this.droppedChunks,
      underflows: this.underflows,
      receivedChunks: this.receivedChunks
    });
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const left = output[0];
    const right = output[1] || output[0];
    const bufferedAtStart = this.bufferedFrames();

    if (!this.playbackStarted) {
      if (bufferedAtStart < this.startBufferedFrames) {
        left.fill(0);
        right.fill(0);
        this.postStatsIfNeeded(left.length);
        return true;
      }
      this.playbackStarted = true;
    }

    for (let i = 0; i < left.length; i++) {
      if (this.queue.length === 0) {
        left[i] = 0;
        right[i] = 0;
        if (this.playbackStarted && !this.inUnderflow) {
          this.underflows++;
          this.inUnderflow = true;
          this.playbackStarted = false;
        }
        continue;
      }

      this.inUnderflow = false;
      const chunk = this.queue[0];
      const base = this.readFrameOffset * this.channels;

      if (base >= chunk.length) {
        this.queue.shift();
        this.readFrameOffset = 0;
        i--;
        continue;
      }

      const l = chunk[base] / 32768.0;
      const r = this.channels >= 2 && base + 1 < chunk.length
        ? chunk[base + 1] / 32768.0
        : l;

      left[i] = l;
      right[i] = r;

      this.readFrameOffset++;

      if (this.readFrameOffset >= Math.floor(chunk.length / this.channels)) {
        this.queue.shift();
        this.readFrameOffset = 0;
      }
    }

    this.postStatsIfNeeded(left.length);
    return true;
  }
}

registerProcessor("pcm-player", PCMPlayerProcessor);
`;

function setStatus(text) {
	const el = document.getElementById("status");
	if (el) el.textContent = text;
	console.log(text);
}

function setStatusDetail(text) {
	const el = document.getElementById("status-detail");
	if (el) el.textContent = text;
}

function setStats(text) {
	const el = document.getElementById("stats");
	if (el) el.textContent = text;
}

function setStatsDetail(text) {
	const el = document.getElementById("stats-detail");
	if (el) el.textContent = text;
}

function clearReconnectTimer() {
	if (!reconnectTimer) return;
	window.clearTimeout(reconnectTimer);
	reconnectTimer = null;
}

function sendPlaybackStats(msg) {
	if (!websocket || websocket.readyState !== WebSocket.OPEN) return;

	try {
		websocket.send(JSON.stringify({
			type: "playbackStats",
			bufferedFrames: msg.bufferedFrames,
			droppedChunks: msg.droppedChunks,
			underflows: msg.underflows,
			receivedChunks: msg.receivedChunks
		}));
	} catch (err) {
		console.warn("Playback stats send failed:", err);
	}
}

function resetPlaybackBuffer() {
	if (!workletNode) return;

	try {
		workletNode.port.postMessage({ type: "reset" });
	} catch (err) {
		console.warn("Playback reset failed:", err);
	}
}

async function hideSplashScreen() {
	const splashScreen = window.Capacitor?.Plugins?.SplashScreen;
	if (!splashScreen?.hide) return;

	try {
		await splashScreen.hide();
	} catch (err) {
		console.warn("Splash hide failed:", err);
	}
}

async function keepNativeBackgroundAudioAlive() {
	const plugin = window.Capacitor?.Plugins?.BackgroundAudio;
	if (!plugin?.keepAlive) return;
	if (backgroundKeepAliveEnabled) return;

	try {
		await plugin.keepAlive();
		backgroundKeepAliveEnabled = true;
	} catch (err) {
		console.warn("Background keep-alive failed:", err);
	}
}

async function releaseNativeBackgroundAudio() {
	const plugin = window.Capacitor?.Plugins?.BackgroundAudio;
	if (!plugin?.release) return;

	try {
		await plugin.release();
	} catch (err) {
		console.warn("Background release failed:", err);
	} finally {
		backgroundKeepAliveEnabled = false;
	}
}

async function ensureAudioContextRunning() {
	if (!started || !audioContext || audioContext.state === "running") return;

	try {
		await audioContext.resume();
	} catch (err) {
		console.warn("AudioContext resume failed:", err);
	}
}

function startResumeWatchdog() {
	stopResumeWatchdog();
	resumeTimer = window.setInterval(() => {
		ensureAudioContextRunning();
	}, 2000);
}

function stopResumeWatchdog() {
	if (!resumeTimer) return;
	window.clearInterval(resumeTimer);
	resumeTimer = null;
}

async function createAudioGraph() {
	audioContext = new AudioContext({
		sampleRate: SAMPLE_RATE,
		latencyHint: "interactive"
	});

	audioContext.onstatechange = () => {
		if (started && audioContext.state === "suspended") {
			ensureAudioContextRunning();
		}
	};

	if (audioContext.state !== "running") {
		await audioContext.resume();
	}

	const blob = new Blob([workletSource], { type: "application/javascript" });
	const workletUrl = URL.createObjectURL(blob);

	try {
		await audioContext.audioWorklet.addModule(workletUrl);
	} finally {
		URL.revokeObjectURL(workletUrl);
	}

	workletNode = new AudioWorkletNode(audioContext, "pcm-player", {
		numberOfInputs: 0,
		numberOfOutputs: 1,
		outputChannelCount: [CHANNELS],
		processorOptions: {
			channels: CHANNELS,
			targetBufferedFrames: TARGET_BUFFERED_FRAMES,
			maxBufferedFrames: MAX_BUFFERED_FRAMES,
			startBufferedFrames: START_BUFFERED_FRAMES
		}
	});

	workletNode.port.onmessage = (event) => {
		const msg = event.data;
		if (!msg || msg.type !== "stats") return;

		const displayFrames = msg.displayBufferedFrames ?? msg.bufferedFrames;
		const ms = (displayFrames * 1000 / SAMPLE_RATE).toFixed(1);
		setStats(`buffer=${ms}ms`);
		setStatsDetail(
			`drops=${msg.droppedChunks}  underflows=${msg.underflows}  received=${msg.receivedChunks}`
		);
		sendPlaybackStats(msg);
	};

	workletNode.connect(audioContext.destination);
}

function updateConnectionUi(message, detail) {
	setStatus(message);
	if (detail) {
		setStatusDetail(detail);
	}
}

async function startAudioStream() {
	if (started) return;

	userStopped = false;
	started = true;
	reconnectAttempts = 0;
	clearReconnectTimer();

	try {
		updateConnectionUi("starting", "Prepping audio graph and opening the socket.");

		if (!audioContext) {
			await createAudioGraph();
		}

		await keepNativeBackgroundAudioAlive();
		startResumeWatchdog();

		connectWebSocket();
	} catch (err) {
		console.error(err);
		updateConnectionUi("error", err.message || String(err));
		started = false;
		await stopAudioStream();
	}
}

function connectWebSocket() {
	if (!started || userStopped) return;

	clearReconnectTimer();

	const currentToken = ++connectionToken;
	const previousSocket = websocket;
	websocket = null;

	if (previousSocket) {
		try {
			previousSocket.onopen = null;
			previousSocket.onmessage = null;
			previousSocket.onerror = null;
			previousSocket.onclose = null;
			previousSocket.close();
		} catch (_) {}
	}

	const socket = new WebSocket(WS_URL);
	websocket = socket;
	socket.binaryType = "arraybuffer";

	updateConnectionUi("connecting", `Attempting ${WS_URL}`);

	socket.onopen = () => {
		if (websocket !== socket || currentToken !== connectionToken) return;

		reconnectAttempts = 0;
		clearReconnectTimer();
		updateConnectionUi("connected", `Stream live through ${WS_URL}`);
		resetPlaybackBuffer();
		ensureAudioContextRunning();
	};

	socket.onmessage = (event) => {
		if (websocket !== socket || currentToken !== connectionToken) return;
		if (!(event.data instanceof ArrayBuffer)) return;

		if (!workletNode) {
			return;
		}

		ensureAudioContextRunning();
		workletNode.port.postMessage(event.data, [event.data]);
	};

	socket.onerror = (err) => {
		if (websocket !== socket || currentToken !== connectionToken) return;
		console.error("WebSocket error:", err);
		updateConnectionUi("socket error", "Waiting for the reconnect cycle.");
	};

	socket.onclose = () => {
		if (websocket !== socket || currentToken !== connectionToken) return;

		websocket = null;
		resetPlaybackBuffer();

		if (userStopped || !started) {
			updateConnectionUi("stopped", "Stream halted locally.");
			return;
		}

		scheduleReconnect();
	};
}

function scheduleReconnect() {
	if (userStopped || !started) return;
	if (reconnectTimer) return;

	reconnectAttempts++;
	const delay = Math.min(RECONNECT_BASE_DELAY * reconnectAttempts, RECONNECT_MAX_DELAY);
	updateConnectionUi("reconnecting", `Retry in ${delay}ms. Attempt ${reconnectAttempts}.`);

	reconnectTimer = window.setTimeout(() => {
		reconnectTimer = null;

		if (userStopped || !started) return;

		ensureAudioContextRunning();
		keepNativeBackgroundAudioAlive();
		connectWebSocket();
	}, delay);
}

async function stopAudioStream() {
	userStopped = true;
	started = false;
	reconnectAttempts = 0;
	clearReconnectTimer();
	stopResumeWatchdog();

	const socket = websocket;
	websocket = null;
	if (socket) {
		try {
			socket.onopen = null;
			socket.onmessage = null;
			socket.onerror = null;
			socket.onclose = null;
			socket.close();
		} catch (_) {}
	}

	if (workletNode) {
		try {
			workletNode.disconnect();
		} catch (_) {}
		workletNode = null;
	}

	if (audioContext) {
		try {
			await audioContext.close();
		} catch (_) {}
		audioContext = null;
	}

	await releaseNativeBackgroundAudio();
	updateConnectionUi("stopped", "Tap Start to reconnect.");
}

function handleAppPaused() {
	if (!started) return;

	// Em background, o Android pode suspender a WebView; mantemos o serviço nativo vivo
	// e tentamos manter o contexto de áudio pronto para retomar imediatamente.
	keepNativeBackgroundAudioAlive();
	ensureAudioContextRunning();
}

function handleAppResumed() {
	if (!started && !userStopped) {
		startAudioStream();
		return;
	}

	if (!started) return;

	keepNativeBackgroundAudioAlive();
	ensureAudioContextRunning();
	startResumeWatchdog();

	if (!websocket || websocket.readyState === WebSocket.CLOSED) {
		scheduleReconnect();
	}
}

class CapacitorWelcome extends HTMLElement {
	connectedCallback() {
		if (this.dataset.rendered === "true") return;
		this.dataset.rendered = "true";

		this.innerHTML = `
			<div class="app-shell">
				<div class="ambient ambient-a"></div>
				<div class="ambient ambient-b"></div>
				<div class="ambient ambient-c"></div>

				<main class="card">
					<div class="card-topline">
						<span class="eyebrow">Capacitor audio relay</span>
						<span class="chip">loopback · adb reverse</span>
					</div>

					<header class="hero">
						<p class="kicker">Audio Phone Speaker</p>
						<h1>Controle um stream de áudio estável e silenciosamente persistente.</h1>
						<p class="lede">
							Recebe PCM 48 kHz do WebSocket local, mantém buffer, reage a pausas do Android
							e evita a reconexão em cascata que travava o fluxo.
						</p>
					</header>

					<section class="metrics">
						<article class="metric metric-primary">
							<span class="metric-label">Conexão</span>
							<strong id="status">ready</strong>
							<p id="status-detail">Pronto para iniciar o stream.</p>
						</article>

						<article class="metric">
							<span class="metric-label">Buffer</span>
							<strong id="stats">-</strong>
							<p id="stats-detail">Aguardando áudio.</p>
						</article>

						<article class="metric">
							<span class="metric-label">Rota</span>
							<strong>127.0.0.1:5001</strong>
							<p>ADB reverse + serviço nativo em foreground</p>
						</article>
					</section>

					<section class="controls" aria-label="Audio controls">
						<button id="start" type="button" class="btn btn-primary">Start stream</button>
						<button id="stop" type="button" class="btn btn-secondary">Stop stream</button>
					</section>

					<div class="footnote">
						Se a WebView oscilar, o app retoma sem abrir sockets duplicados.
					</div>
				</main>
			</div>
		`;

		window.setTimeout(() => {
			startAudioStream();
		}, 0);
	}
}

if (!customElements.get("capacitor-welcome")) {
	customElements.define("capacitor-welcome", CapacitorWelcome);
}

window.startAudioStream = startAudioStream;
window.stopAudioStream = stopAudioStream;

window.addEventListener("DOMContentLoaded", () => {
	hideSplashScreen();

	const startButton =
		document.getElementById("start") ||
		document.getElementById("startButton") ||
		document.querySelector("[data-audio-start]");

	const stopButton =
		document.getElementById("stop") ||
		document.getElementById("stopButton") ||
		document.querySelector("[data-audio-stop]");

	if (startButton) {
		startButton.addEventListener("click", () => {
			startAudioStream();
		});
	}

	if (stopButton) {
		stopButton.addEventListener("click", () => {
			stopAudioStream();
		});
	}

	if (!started) {
		updateConnectionUi("ready", "Tap Start to open the local WebSocket.");
		setStats("-");
		setStatsDetail("Aguardando áudio.");
	}
});

document.addEventListener("visibilitychange", () => {
	if (document.visibilityState === "hidden") {
		handleAppPaused();
		return;
	}

	handleAppResumed();
	ensureAudioContextRunning();
});

window.addEventListener("focus", () => {
	handleAppResumed();
});

document.addEventListener("pause", handleAppPaused, false);
document.addEventListener("resume", handleAppResumed, false);

window.addEventListener("blur", () => {
	if (!started) return;

	keepNativeBackgroundAudioAlive();
	ensureAudioContextRunning();
});
