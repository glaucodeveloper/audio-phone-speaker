package glauco.phone.audiospeaker;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.WebView;

import androidx.annotation.NonNull;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.BridgeActivity;

import java.util.ArrayList;
import java.util.List;

public class MainActivity extends BridgeActivity {
    private static final int REQUEST_RUNTIME_PERMISSIONS = 1001;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(BackgroundAudioPlugin.class);
        super.onCreate(savedInstanceState);
        configureWebViewAudio();
        requestBatteryOptimizationExemptionIfNeeded();
        requestPermissionsAndStartService();
    }

    @Override
    public void onPause() {
        configureWebViewAudio();
        if (bridge != null && bridge.getWebView() != null) {
            bridge.getWebView().onWindowFocusChanged(true);
        }
        super.onPause();
    }

    @Override
    public void onResume() {
        super.onResume();
        configureWebViewAudio();
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED) {
            startBackgroundAudioService();
        }
    }

    @Override
    public void onRequestPermissionsResult(
        int requestCode,
        @NonNull String[] permissions,
        @NonNull int[] grantResults
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_RUNTIME_PERMISSIONS) {
            // Speaker mode remains available even if microphone permission was denied.
            startBackgroundAudioService();
        }
    }

    private void configureWebViewAudio() {
        if (bridge == null || bridge.getWebView() == null) return;
        WebView webView = bridge.getWebView();
        webView.getSettings().setMediaPlaybackRequiresUserGesture(false);
    }

    private void requestPermissionsAndStartService() {
        List<String> missing = new ArrayList<>();
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.RECORD_AUDIO);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.POST_NOTIFICATIONS);
        }

        if (missing.isEmpty()) {
            startBackgroundAudioService();
        } else {
            ActivityCompat.requestPermissions(
                this,
                missing.toArray(new String[0]),
                REQUEST_RUNTIME_PERMISSIONS
            );
        }
    }

    private void startBackgroundAudioService() {
        Intent intent = new Intent(this, BackgroundAudioService.class);
        intent.setAction(BackgroundAudioService.ACTION_START_DUPLEX);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void requestBatteryOptimizationExemptionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return;
        PowerManager manager = getSystemService(PowerManager.class);
        if (manager == null || manager.isIgnoringBatteryOptimizations(getPackageName())) return;
        Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
        intent.setData(Uri.parse("package:" + getPackageName()));
        if (intent.resolveActivity(getPackageManager()) != null) startActivity(intent);
    }
}
