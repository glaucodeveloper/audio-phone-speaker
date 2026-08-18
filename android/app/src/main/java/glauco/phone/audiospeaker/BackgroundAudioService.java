package glauco.phone.audiospeaker;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import androidx.core.app.NotificationCompat;

public class BackgroundAudioService extends Service {
    private static final String TAG = "BackgroundAudioService";
    public static final String ACTION_START = "glauco.phone.audiospeaker.action.START_BACKGROUND_AUDIO";
    public static final String ACTION_START_DUPLEX = "glauco.phone.audiospeaker.action.START_DUPLEX_AUDIO";
    public static final String ACTION_STOP = "glauco.phone.audiospeaker.action.STOP_BACKGROUND_AUDIO";
    private static final int NOTIFICATION_ID = 5000;
    private static final String CHANNEL_ID = "audio_stream";

    private PowerManager.WakeLock wakeLock;
    private PhoneSpeakerBridge speakerBridge;
    private PhoneMicrophoneBridge microphoneBridge;

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "onCreate service=" + System.identityHashCode(this));
        createNotificationChannel();
        acquireWakeLock();
        speakerBridge = new PhoneSpeakerBridge();
        microphoneBridge = new PhoneMicrophoneBridge(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? "<null>" : String.valueOf(intent.getAction());
        Log.i(TAG, "onStartCommand action=" + action + " startId=" + startId);
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            Log.w(TAG, "Ignoring legacy ACTION_STOP; native audio service stays alive");
            return START_STICKY;
        }

        boolean duplexRequested = intent != null && ACTION_START_DUPLEX.equals(intent.getAction());
        boolean microphoneActive = duplexRequested &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;

        Notification notification = buildNotification(microphoneActive);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
            if (microphoneActive) types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            startForeground(NOTIFICATION_ID, notification, types);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }

        acquireWakeLock();
        if (speakerBridge != null) speakerBridge.start();
        if (microphoneBridge != null) {
            microphoneBridge.setMicrophoneForegroundActive(microphoneActive);
            microphoneBridge.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        Log.e(TAG, "onDestroy service=" + System.identityHashCode(this));
        if (speakerBridge != null) speakerBridge.stop();
        if (microphoneBridge != null) microphoneBridge.stop();
        releaseWakeLock();
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        Log.w(TAG, "onTaskRemoved; keeping foreground audio service alive");
        super.onTaskRemoved(rootIntent);
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private Notification buildNotification(boolean microphoneActive) {
        Intent launchIntent = new Intent(this, MainActivity.class);
        launchIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            this, 0, launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        String text = microphoneActive
            ? "Speaker e microfone conectados ao computador"
            : "Speaker conectado ao computador";
        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.background_audio_notification_title))
            .setContentText(text)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            getString(R.string.background_audio_channel_name),
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setSound(null, null);
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) return;
        PowerManager manager = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (manager == null) return;
        wakeLock = manager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "audio-phone-speaker:NativeDuplexAudio"
        );
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire();
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
    }
}
