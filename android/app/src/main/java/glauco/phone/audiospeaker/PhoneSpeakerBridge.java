package glauco.phone.audiospeaker;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Build;
import android.os.Process;
import android.util.Log;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

/** Native PC -> phone PCM relay over adb reverse tcp/5001. */
public final class PhoneSpeakerBridge {
    private static final String TAG = "PhoneSpeakerBridge";
    private static final String HOST = "127.0.0.1";
    private static final int PORT = 5001;
    private static final int SAMPLE_RATE = 48000;
    private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_OUT_STEREO;
    private static final int AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT;
    private static final int BYTES_PER_FRAME = 4;
    // Low-latency playback: 40 ms initial prime, ~80 ms track target.
    private static final int START_BUFFER_BYTES = SAMPLE_RATE * BYTES_PER_FRAME * 40 / 1000;
    private static final int TARGET_TRACK_BUFFER_BYTES = SAMPLE_RATE * BYTES_PER_FRAME * 80 / 1000;

    private volatile boolean running;
    private volatile Socket socket;
    private volatile AudioTrack audioTrack;
    private Thread connectionThread;

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
            try (Socket connected = new Socket()) {
                connected.setTcpNoDelay(true);
                connected.setKeepAlive(true);
                connected.setReceiveBufferSize(256 * 1024);
                connected.connect(new InetSocketAddress(HOST, PORT), 4000);
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

                while (running && !connected.isClosed()) {
                    writeFully(track, readFrame(input));
                }
            } catch (Throwable error) {
                if (running) Log.e(TAG, "Speaker transport failure; reconnecting", error);
            } finally {
                socket = null;
                if (track != null) releaseAudioTrack(track);
            }

            if (running) {
                try { Thread.sleep(500); }
                catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
            }
        }
    }

    private AudioTrack createAudioTrack() {
        int minimum = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT);
        int bufferSize = Math.max(minimum * 2, TARGET_TRACK_BUFFER_BYTES);

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
