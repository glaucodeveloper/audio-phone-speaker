package glauco.phone.audiospeaker;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Process;
import android.util.Log;

import org.json.JSONObject;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/** Native, foreground-safe phone microphone relay over adb reverse. */
public final class PhoneMicrophoneBridge {
    private static final String TAG = "PhoneMicrophoneBridge";
    private static final String HOST = "127.0.0.1";
    private static final int PORT = 5002;
    private static final int SAMPLE_RATE = 16000;
    private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO;
    private static final int AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT;
    private static final int TYPE_CONTROL = 1;
    private static final int TYPE_PCM = 2;

    private final Context context;
    private final Object writeLock = new Object();
    private volatile boolean running;
    private volatile boolean recording;
    private volatile Socket socket;
    private volatile DataOutputStream output;
    private volatile AudioRecord audioRecord;
    private volatile boolean microphoneForegroundActive;
    private Thread connectionThread;
    private Thread recordingThread;

    public PhoneMicrophoneBridge(Context context) {
        this.context = context.getApplicationContext();
    }

    public void setMicrophoneForegroundActive(boolean active) {
        microphoneForegroundActive = active;
        if (!active) stopRecording();
    }

    public synchronized void start() {
        if (running) return;
        running = true;
        connectionThread = new Thread(this::connectionLoop, "phone-mic-connection");
        connectionThread.setDaemon(true);
        connectionThread.start();
    }

    public synchronized void stop() {
        running = false;
        stopRecording();
        closeSocket();
        if (connectionThread != null) connectionThread.interrupt();
        connectionThread = null;
    }

    private void connectionLoop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        while (running) {
            try (Socket connected = new Socket()) {
                connected.setTcpNoDelay(true);
                connected.setKeepAlive(true);
                connected.connect(new InetSocketAddress(HOST, PORT), 4000);
                socket = connected;
                DataInputStream input = new DataInputStream(connected.getInputStream());
                output = new DataOutputStream(connected.getOutputStream());
                sendControl(new JSONObject()
                    .put("type", "hello")
                    .put("role", "phone-microphone")
                    .put("sampleRate", SAMPLE_RATE)
                    .put("channels", 1)
                    .put("format", "pcm_s16le"));

                while (running && !connected.isClosed()) {
                    int frameLength = input.readInt();
                    if (frameLength < 1 || frameLength > 1024 * 1024) {
                        throw new IllegalStateException("Invalid control frame length: " + frameLength);
                    }
                    int frameType = input.readUnsignedByte();
                    byte[] payload = new byte[frameLength - 1];
                    input.readFully(payload);
                    if (frameType == TYPE_CONTROL) {
                        handleControl(new JSONObject(new String(payload, StandardCharsets.UTF_8)));
                    }
                }
            } catch (Exception error) {
                if (running) Log.w(TAG, "Microphone bridge disconnected", error);
            } finally {
                stopRecording();
                output = null;
                socket = null;
            }

            if (running) {
                try {
                    Thread.sleep(1500);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
            }
        }
    }

    private void handleControl(JSONObject message) {
        String type = message.optString("type", "");
        if ("startMic".equals(type)) {
            startRecording();
        } else if ("stopMic".equals(type)) {
            stopRecording();
        } else if ("ping".equals(type)) {
            sendStatus("ready", null);
        }
    }

    private synchronized void startRecording() {
        if (recording) return;
        if (!microphoneForegroundActive) {
            sendStatus("error", "Abra o app para ativar o serviço de microfone em primeiro plano");
            return;
        }
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            sendStatus("error", "RECORD_AUDIO permission not granted");
            return;
        }

        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT);
        int bufferSize = Math.max(minimum, SAMPLE_RATE * 2 / 5);
        AudioRecord recorder = new AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            .setAudioFormat(new AudioFormat.Builder()
                .setEncoding(AUDIO_FORMAT)
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(CHANNEL_CONFIG)
                .build())
            .setBufferSizeInBytes(bufferSize)
            .build();

        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            recorder.release();
            sendStatus("error", "AudioRecord failed to initialize");
            return;
        }

        audioRecord = recorder;
        recording = true;
        recordingThread = new Thread(() -> recordLoop(recorder, bufferSize), "phone-mic-capture");
        recordingThread.setDaemon(true);
        recordingThread.start();
    }

    private void recordLoop(AudioRecord recorder, int bufferSize) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        byte[] buffer = new byte[Math.max(2048, bufferSize / 2)];
        try {
            recorder.startRecording();
            sendStatus("recording", null);
            while (running && recording) {
                int count = recorder.read(buffer, 0, buffer.length, AudioRecord.READ_BLOCKING);
                if (count > 0) {
                    sendFrame(TYPE_PCM, Arrays.copyOf(buffer, count));
                } else if (count < 0) {
                    throw new IllegalStateException("AudioRecord.read failed: " + count);
                }
            }
        } catch (Exception error) {
            Log.e(TAG, "Microphone capture failed", error);
            sendStatus("error", error.getMessage());
        } finally {
            try {
                if (recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    recorder.stop();
                }
            } catch (Exception ignored) {}
            recorder.release();
            if (audioRecord == recorder) audioRecord = null;
            recording = false;
            sendStatus("stopped", null);
        }
    }

    private synchronized void stopRecording() {
        recording = false;
        AudioRecord recorder = audioRecord;
        if (recorder != null) {
            try {
                if (recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    recorder.stop();
                }
            } catch (Exception ignored) {}
        }
        Thread thread = recordingThread;
        if (thread != null && thread != Thread.currentThread()) {
            try {
                thread.join(800);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        recordingThread = null;
    }

    private void sendStatus(String status, String error) {
        try {
            JSONObject message = new JSONObject()
                .put("type", "micStatus")
                .put("status", status);
            if (error != null && !error.isEmpty()) message.put("error", error);
            sendControl(message);
        } catch (Exception ignored) {}
    }

    private void sendControl(JSONObject object) {
        sendFrame(TYPE_CONTROL, object.toString().getBytes(StandardCharsets.UTF_8));
    }

    private void sendFrame(int type, byte[] payload) {
        DataOutputStream stream = output;
        if (stream == null) return;
        synchronized (writeLock) {
            try {
                stream.writeInt(payload.length + 1);
                stream.writeByte(type);
                stream.write(payload);
                stream.flush();
            } catch (Exception error) {
                Log.w(TAG, "Frame send failed", error);
                closeSocket();
            }
        }
    }

    private void closeSocket() {
        Socket current = socket;
        socket = null;
        if (current != null) {
            try {
                current.close();
            } catch (Exception ignored) {}
        }
    }
}
