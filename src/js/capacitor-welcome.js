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
let backgroundKeepAliveEnabled = false;
let userStopped = false;
let reconnectAttempts = 0;
const RECONNECT_BASE_DELAY = 1000;

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

function setStats(text) {
	const el = document.getElementById("stats");
	if (el) el.textContent = text;
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
		setStats(
			`buffer=${ms}ms dropped=${msg.droppedChunks} underflows=${msg.underflows}`
		);
		sendPlaybackStats(msg);
	};

	workletNode.connect(audioContext.destination);
}

async function startAudioStream() {
	if (started) return;
	userStopped = false;
	started = true;
	reconnectAttempts = 0;

	try {
		setStatus("starting audio...");

		if (!audioContext) {
			await createAudioGraph();
		}
		await keepNativeBackgroundAudioAlive();
		startResumeWatchdog();

		connectWebSocket();
	} catch (err) {
		console.error(err);
		setStatus(`error: ${err.message || err}`);
		started = false;
		await stopAudioStream();
	}
}

function connectWebSocket() {
	if (websocket && websocket.readyState <= WebSocket.OPEN) {
		try { websocket.close(); } catch (_) {}
	}

	websocket = new WebSocket(WS_URL);
	websocket.binaryType = "arraybuffer";

	websocket.onopen = () => {
		reconnectAttempts = 0;
		setStatus(`connected: ${WS_URL}`);
		resetPlaybackBuffer();
		ensureAudioContextRunning();
	};

	websocket.onmessage = (event) => {
		if (!workletNode) {
			if (!audioContext) {
				createAudioGraph().then(() => {
					workletNode.port.postMessage(event.data, [event.data]);
				});
			}
			return;
		}
		if (!(event.data instanceof ArrayBuffer)) return;

		ensureAudioContextRunning();
		workletNode.port.postMessage(event.data, [event.data]);
	};

	websocket.onerror = (err) => {
		console.error("WebSocket error:", err);
		setStatus("websocket error");
	};

	websocket.onclose = () => {
		if (userStopped) return;

		resetPlaybackBuffer();
		setStatus("websocket closed - reconnecting...");
		scheduleReconnect();
	};
}

function scheduleReconnect() {
	if (userStopped || !started) return;

	reconnectAttempts++;
	const delay = Math.min(RECONNECT_BASE_DELAY * reconnectAttempts, 10000);
	setStatus(`reconnecting in ${delay}ms (attempt ${reconnectAttempts})...`);

	setTimeout(() => {
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
	stopResumeWatchdog();

	if (websocket) {
		try {
			websocket.close();
		} catch (_) { }
		websocket = null;
	}

	if (workletNode) {
		try {
			workletNode.disconnect();
		} catch (_) { }
		workletNode = null;
	}

	if (audioContext) {
		try {
			await audioContext.close();
		} catch (_) { }
		audioContext = null;
	}

	await releaseNativeBackgroundAudio();
	setStatus("stopped");
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

	if (!websocket || websocket.readyState >= WebSocket.CLOSING) {
		reconnectAttempts = 0;
		connectWebSocket();
	}
}

class CapacitorWelcome extends HTMLElement {
	connectedCallback() {
		if (this.dataset.rendered === "true") return;
		this.dataset.rendered = "true";

		this.innerHTML = `
			<div style="font-family: system-ui, sans-serif; padding: 24px; max-width: 560px; margin: 0 auto;">
				<h1 style="margin: 0 0 8px; font-size: 24px;">Audio Phone Speaker</h1>
				<p style="margin: 0 0 16px; color: #555;">Status: <span id="status">starting...</span></p>
				<p style="margin: 0 0 20px; color: #555;">Stats: <span id="stats">-</span></p>
				<div style="display: flex; gap: 12px; flex-wrap: wrap;">
					<button id="start" type="button">Start</button>
					<button id="stop" type="button">Stop</button>
				</div>
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

	setStatus("ready");
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
