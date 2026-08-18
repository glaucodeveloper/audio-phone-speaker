const SPEAKER_PORT = 5001;
const MIC_PORT = 5002;
const SAMPLE_RATE = 48000;

function backgroundAudioPlugin() {
  return window.Capacitor?.Plugins?.BackgroundAudio;
}

function setText(id, text) {
  const element = document.getElementById(id);
  if (element) element.textContent = text;
}

function setState(state, detail = "") {
  setText("bridge-state", state);
  setText("bridge-detail", detail);
}

async function hideSplash() {
  const splash = window.Capacitor?.Plugins?.SplashScreen;
  if (!splash?.hide) return;

  try {
    await splash.hide();
  } catch (error) {
    console.warn("Splash hide failed:", error);
  }
}

async function keepNativeBridgeAlive() {
  const plugin = backgroundAudioPlugin();

  if (!plugin?.keepAlive) {
    setState(
      "plugin indisponível",
      "BackgroundAudio não foi registrado no runtime Capacitor."
    );
    return;
  }

  setState(
    "ativando",
    "Iniciando foreground service nativo."
  );

  try {
    await plugin.keepAlive();

    setState(
      "ativo",
      "AudioTrack + AudioRecord conectam diretamente às portas ADB reverse."
    );
  } catch (error) {
    console.error("BackgroundAudio.keepAlive failed:", error);

    setState(
      "erro",
      error?.message || String(error)
    );
  }
}

class CapacitorWelcome extends HTMLElement {
  connectedCallback() {
    this.innerHTML = `
      <main class="app-shell">
        <section class="card">
          <header class="hero">
            <div class="topbar">
              <div>
                <p class="eyebrow">Android / Capacitor</p>
                <h1>Audio Phone Speaker</h1>
              </div>
              <div class="header-stacks">
                <span class="native-tag">Native TCP</span>
                <span class="native-tag">48 kHz</span>
                <span class="native-tag">Full duplex</span>
              </div>
            </div>

            <p class="lede">
              O WebView funciona apenas como interface. O áudio é tratado
              nativamente pelo foreground service Android: AudioTrack recebe
              o som do computador e AudioRecord envia o microfone do telefone.
            </p>
          </header>

          <section class="dashboard-overview">
            <article class="panel">
              <div class="tile-header">
                <div>
                  <p class="metric-label">Estado</p>
                  <strong id="bridge-state">iniciando</strong>
                </div>
              </div>

              <p id="bridge-detail">
                Solicitando o foreground service.
              </p>

              <div class="structured-list">
                <div>
                  <span>PC → telefone</span>
                  <strong>TCP ${SPEAKER_PORT}</strong>
                </div>
                <div>
                  <span>Telefone → PC</span>
                  <strong>TCP ${MIC_PORT}</strong>
                </div>
                <div>
                  <span>Speaker</span>
                  <strong>${SAMPLE_RATE} Hz / stereo / PCM16</strong>
                </div>
                <div>
                  <span>Microfone</span>
                  <strong>${SAMPLE_RATE} Hz / mono / PCM16</strong>
                </div>
              </div>
            </article>

            <article class="panel">
              <p class="metric-label">Transporte</p>
              <strong>ADB reverse + foreground service</strong>
              <p>
                Não existe WebSocket ou AudioWorklet no caminho de áudio.
                O Java nativo reconecta automaticamente a
                <code>127.0.0.1</code>.
              </p>

              <div class="controls">
                <button id="reconnect-button" class="native-button" type="button">
                  Reativar ponte
                </button>
              </div>
            </article>
          </section>

          <p class="footnote">
            Mantenha a depuração USB autorizada. O processo Python no computador
            configura <code>adb reverse tcp:5001</code> e
            <code>adb reverse tcp:5002</code>.
          </p>
        </section>
      </main>
    `;

    document
      .getElementById("reconnect-button")
      ?.addEventListener(
        "click",
        () => keepNativeBridgeAlive()
      );

    hideSplash();
    keepNativeBridgeAlive();
  }
}

customElements.define(
  "capacitor-welcome",
  CapacitorWelcome
);

document.addEventListener(
  "visibilitychange",
  () => {
    if (!document.hidden) {
      keepNativeBridgeAlive();
    }
  }
);

window.addEventListener(
  "focus",
  () => keepNativeBridgeAlive()
);
