package glauco.phone.audiospeaker;

import android.content.Intent;
import android.os.Build;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "BackgroundAudio")
public class BackgroundAudioPlugin extends Plugin {
    @PluginMethod
    public void keepAlive(PluginCall call) {
        Intent intent = new Intent(getContext(), BackgroundAudioService.class);
        // Called from the visible WebView after RECORD_AUDIO was granted.
        intent.setAction(BackgroundAudioService.ACTION_START_DUPLEX);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getContext().startForegroundService(intent);
        } else {
            getContext().startService(intent);
        }
        JSObject result = new JSObject();
        result.put("active", true);
        call.resolve(result);
    }

    @PluginMethod
    public void release(PluginCall call) {
        JSObject result = new JSObject();
        result.put("active", true);
        result.put("persistent", true);
        call.resolve(result);
    }
}
