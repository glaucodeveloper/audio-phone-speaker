package glauco.phone.audiospeaker;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Build;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;

/** Native PC -> phone PCM relay over adb reverse tcp/5001. */
public final class PhoneSpeakerBridge {
    private static final String TAG = "PhoneSpeakerBridge";
    private static final String HOST = "127.0.0.1";
    private static final int PORT = 5001;
    private static final int SAMPLE_RATE = 48000;
    private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_OUT_STEREO;
    private static final int AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT;
    private static final int BYTES_PER_FRAME = 4;
    private static final int FRAME_HEADER_BYTES = 4;
    private static final int FRAME_DURATION_MS = 10;
    private static final int SOCKET_READ_TIMEOUT_MS = 100;
    private static final int MAX_SOCKET_BACKLOG_FRAMES = 1;
    private static final int MAX_SOCKET_BACKLOG_BYTES = MAX_SOCKET_BACKLOG_FRAMES
        * (FRAME_HEADER_BYTES + SAMPLE_RATE * BYTES_PER_FRAME * FRAME_DURATION_MS / 1000);
    private static final int UNDERFLOWS_BEFORE_RECONNECT = 1;
    private static final long UNDERFLOW_RECONNECT_COOLDOWN_MS = 1000;
    private static final long MIN_TRANSPORT_RECONNECT_DELAY_MS = 250;
    private static final long MAX_TRANSPORT_RECONNECT_DELAY_MS = 2000;
    // Prime enough audio to cover Android's minimum AudioTrack buffer before play().
    // Four 10 ms packets avoid starting playback below the device's observed 32 ms minimum.
    private static final int START_BUFFER_BYTES = SAMPLE_RATE * BYTES_PER_FRAME * 40 / 1000;
    private static final int TARGET_TRACK_BUFFER_BYTES = SAMPLE_RATE * BYTES_PER_FRAME * 40 / 1000;

    private volatile boolean running;
    private volatile Socket socket;
    private volatile AudioTrack audioTrack;
    private Thread connectionThread;
    private long lastBacklogLogAtMs;
    private long lastUnderflowReconnectAtMs;
    private long transportReconnectDelayMs = MIN_TRANSPORT_RECONNECT_DELAY_MS;

    private static final class AudioTrackUnderflowException extends Exception {
        AudioTrackUnderflowException(int count) {
            super("AudioTrack underrun count increased to " + count);
        }
    }

    public synchronized void start() {
        if (running) return;
        running = true;
        connectionThread = new Thread(this::connectionLoop, "phone-speaker-connection");
        connectionThread.setDaemon(true);
        connectionThread.start();
    }

    public synchronized void stop() {
        running = false;
        closeSocket();
        releaseAudioTrack();
        if (connectionThread != null) connectionThread.interrupt();
        connectionThread = null;
    }

    private void connectionLoop() {
        try {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
        } catch (Throwable priorityError) {
            Log.w(TAG, "Could not set audio thread priority", priorityError);
        }
        while (running) {
            AudioTrack track = null;
            boolean underflowRecovery = false;
            boolean staleTransportRecovery = false;
            long connectionStartedAtMs = 0;
            try (Socket connected = new Socket()) {
                connected.setTcpNoDelay(true);
                connected.setKeepAlive(true);
                // ADB reverse can leave a half-open socket after USB drops.
                // Bound every packet read so stale playback is flushed and
                // the bridge reconnects to the latest PCM instead of waiting
                // forever on an obsolete connection.
                connected.setSoTimeout(SOCKET_READ_TIMEOUT_MS);
                connected.setReceiveBufferSize(MAX_SOCKET_BACKLOG_BYTES);
                connected.connect(new InetSocketAddress(HOST, PORT), 4000);
                connectionStartedAtMs = SystemClock.elapsedRealtime();
                socket = connected;

                // Authenticate the native speaker transport before PCM starts.
                // This prevents the legacy WebView WebSocket client from sharing
                // tcp/5001 with the native AudioTrack bridge.
                DataOutputStream output = new DataOutputStream(connected.getOutputStream());
                output.write(new byte[] { 'S', 'P', 'K', '1' });
                output.flush();

                DataInputStream input = new DataInputStream(connected.getInputStream());
                track = createAudioTrack();
                audioTrack = track;

                int primed = 0;
                while (running && primed < START_BUFFER_BYTES) {
                    byte[] pcm = readFrame(input);
                    writeFully(track, pcm);
                    primed += pcm.length;
                }

                if (!running) break;
                track.play();
                Log.i(TAG, "Speaker connected; prebuffered=" + primed);
                int lastUnderrunCount = readUnderrunCount(track);
                int underflowsSinceConnection = 0;

                while (running && !connected.isClosed()) {
                    writeFully(track, readFreshFrame(input));
                    int currentUnderrunCount = readUnderrunCount(track);
                    if (currentUnderrunCount > lastUnderrunCount) {
                        underflowsSinceConnection += currentUnderrunCount - lastUnderrunCount;
                        long now = SystemClock.elapsedRealtime();
                        if (underflowsSinceConnection >= UNDERFLOWS_BEFORE_RECONNECT
                            && (lastUnderflowReconnectAtMs == 0
                                || now - lastUnderflowReconnectAtMs
                                    >= UNDERFLOW_RECONNECT_COOLDOWN_MS)) {
                            underflowRecovery = true;
                            lastUnderflowReconnectAtMs = now;
                            throw new AudioTrackUnderflowException(currentUnderrunCount);
                        }
                    }
                    lastUnderrunCount = currentUnderrunCount;
                }
            } catch (SocketTimeoutException error) {
                staleTransportRecovery = true;
                if (running) {
                    Log.w(
                        TAG,
                        "Speaker packet timeout; discarding stale playback and reconnecting"
                    );
                }
            } catch (Throwable error) {
                underflowRecovery = underflowRecovery
                    || error instanceof AudioTrackUnderflowException;
                if (running && error instanceof AudioTrackUnderflowException) {
                    Log.w(TAG, "AudioTrack underflow; reconnecting for fresh PCM", error);
                } else if (running) {
                    Log.e(TAG, "Speaker transport failure; reconnecting", error);
                }
            } finally {
                socket = null;
                if (track != null) releaseAudioTrack(track);
            }

            if (running) {
                long reconnectDelayMs = 100;
                if (!underflowRecovery && !staleTransportRecovery) {
                    long now = SystemClock.elapsedRealtime();
                    if (connectionStartedAtMs > 0
                        && now - connectionStartedAtMs >= 10000) {
                        transportReconnectDelayMs = MIN_TRANSPORT_RECONNECT_DELAY_MS;
                    }
                    reconnectDelayMs = transportReconnectDelayMs;
                    transportReconnectDelayMs = Math.min(
                        transportReconnectDelayMs * 2,
                        MAX_TRANSPORT_RECONNECT_DELAY_MS
                    );
                }
                try { Thread.sleep(reconnectDelayMs); }
                catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
            }
        }
    }

    private int readUnderrunCount(AudioTrack track) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            return track.getUnderrunCount();
        }
        return 0;
    }

    private AudioTrack createAudioTrack() {
        int minimum = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT);
        int bufferSize = Math.max(minimum, TARGET_TRACK_BUFFER_BYTES);

        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
            .build();
        AudioFormat format = new AudioFormat.Builder()
            .setEncoding(AUDIO_FORMAT)
            .setSampleRate(SAMPLE_RATE)
            .setChannelMask(CHANNEL_CONFIG)
            .build();

        AudioTrack.Builder builder = new AudioTrack.Builder()
            .setAudioAttributes(attributes)
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufferSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setSessionId(AudioManager.AUDIO_SESSION_ID_GENERATE);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY);
        }

        AudioTrack track = builder.build();
        if (track.getState() != AudioTrack.STATE_INITIALIZED) {
            track.release();
            throw new IllegalStateException("AudioTrack failed to initialize");
        }
        Log.i(
            TAG,
            "AudioTrack buffer="
                + (track.getBufferSizeInFrames() * 1000 / SAMPLE_RATE)
                + "ms, minimum=" + minimum + " bytes, requested=" + bufferSize + " bytes"
        );
        return track;
    }

    private byte[] readFrame(DataInputStream input) throws Exception {
        int length = input.readInt();
        if (length < BYTES_PER_FRAME || length > 1024 * 1024 || (length % BYTES_PER_FRAME) != 0) {
            throw new IllegalStateException("Invalid PCM frame length: " + length);
        }
        byte[] payload = new byte[length];
        input.readFully(payload);
        return payload;
    }

    private byte[] readFreshFrame(DataInputStream input) throws Exception {
        byte[] pcm = readFrame(input);
        int queuedBytes = input.available();
        int droppedFrames = 0;

        while (queuedBytes > MAX_SOCKET_BACKLOG_BYTES) {
            pcm = readFrame(input);
            droppedFrames++;
            queuedBytes = input.available();
        }

        long now = SystemClock.elapsedRealtime();
        if (droppedFrames > 0 && now - lastBacklogLogAtMs >= 1000) {
            lastBacklogLogAtMs = now;
            Log.w(
                TAG,
                "Buffer guard dropped " + droppedFrames
                    + " stale frames; queuedBytes=" + queuedBytes
                    + ", limitBytes=" + MAX_SOCKET_BACKLOG_BYTES
            );
        }

        return pcm;
    }

    private void writeFully(AudioTrack track, byte[] pcm) {
        int offset = 0;
        while (running && offset < pcm.length) {
            int written = track.write(pcm, offset, pcm.length - offset);
            if (written < 0) throw new IllegalStateException("AudioTrack.write failed: " + written);
            if (written == 0) { Thread.yield(); continue; }
            offset += written;
        }
    }

    private synchronized void releaseAudioTrack() {
        AudioTrack current = audioTrack;
        audioTrack = null;
        if (current != null) releaseAudioTrack(current);
    }

    private void releaseAudioTrack(AudioTrack track) {
        if (audioTrack == track) audioTrack = null;
        try { track.pause(); } catch (Exception ignored) {}
        try { track.flush(); } catch (Exception ignored) {}
        try { track.stop(); } catch (Exception ignored) {}
        try { track.release(); } catch (Exception ignored) {}
    }

    private void closeSocket() {
        Socket current = socket;
        socket = null;
        if (current != null) {
            try { current.close(); } catch (Exception ignored) {}
        }
    }
}
