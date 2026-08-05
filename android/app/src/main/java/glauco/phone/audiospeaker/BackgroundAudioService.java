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

import androidx.core.app.NotificationCompat;

public class BackgroundAudioService extends Service {
    public static final String ACTION_START = "glauco.phone.audiospeaker.action.START_BACKGROUND_AUDIO";
    public static final String ACTION_START_DUPLEX = "glauco.phone.audiospeaker.action.START_DUPLEX_AUDIO";
    public static final String ACTION_STOP = "glauco.phone.audiospeaker.action.STOP_BACKGROUND_AUDIO";

    private static final int NOTIFICATION_ID = 5000;
    private static final String CHANNEL_ID = "audio_stream";
    private PowerManager.WakeLock wakeLock;
    private PhoneMicrophoneBridge microphoneBridge;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        acquireWakeLock();
        microphoneBridge = new PhoneMicrophoneBridge(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            if (microphoneBridge != null) microphoneBridge.stop();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        boolean duplexRequested = intent != null && ACTION_START_DUPLEX.equals(intent.getAction());
        boolean microphoneActive = duplexRequested &&
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;

        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            int serviceTypes = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
            if (microphoneActive) {
                serviceTypes |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            }
            startForeground(NOTIFICATION_ID, notification, serviceTypes);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }

        acquireWakeLock();
        if (microphoneBridge != null) {
            microphoneBridge.setMicrophoneForegroundActive(microphoneActive);
            microphoneBridge.start();
        }
        return START_STICKY;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // START_STICKY keeps playback alive. A microphone foreground service must be
        // re-enabled from the visible Activity because RECORD_AUDIO is while-in-use.
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        if (microphoneBridge != null) microphoneBridge.stop();
        releaseWakeLock();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private Notification buildNotification() {
        Intent launchIntent = new Intent(this, MainActivity.class);
        launchIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.background_audio_notification_title))
            .setContentText("Speaker e microfone conectados ao computador")
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            getString(R.string.background_audio_channel_name),
            NotificationManager.IMPORTANCE_LOW
        );
        NotificationManager manager =
            (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) return;
        PowerManager manager = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (manager == null) return;
        wakeLock = manager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "audio-phone-speaker:BackgroundAudio"
        );
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire();
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
    }
}
