#!/usr/bin/env python3
"""Request microphone permission for Python.app -- run once, click Allow."""
import objc
import AppKit
from PyObjCTools import AppHelper

AVFoundation = objc.loadBundle(
    'AVFoundation', globals(),
    bundle_path='/System/Library/Frameworks/AVFoundation.framework',
)

objc.registerMetaDataForSelector(
    b'AVCaptureDevice',
    b'requestAccessForMediaType:completionHandler:',
    {
        'arguments': {
            3: {
                'callable': {
                    'retval': {'type': b'v'},
                    'arguments': {
                        0: {'type': b'^v'},
                        1: {'type': b'Z'},
                    },
                },
            },
        },
    },
)


class MicRequestDelegate(AppKit.NSObject):
    def applicationDidFinishLaunching_(self, notification):
        status = AVCaptureDevice.authorizationStatusForMediaType_('soun')
        print(f"Current mic status: {status} (0=notDetermined, 2=denied, 3=authorized)")
        if status == 3:
            print("Already authorized!")
            AppKit.NSApplication.sharedApplication().terminate_(None)
            return
        print("Requesting mic permission -- look for the macOS dialog...")
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            'soun', self.onGranted_
        )

    def onGranted_(self, granted):
        print(f"Permission granted: {granted}")
        AppKit.NSApplication.sharedApplication().performSelectorOnMainThread_withObject_waitUntilDone_(
            'terminate:', None, False
        )


app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)  # Show in dock so dialog works
delegate = MicRequestDelegate.alloc().init()
app.setDelegate_(delegate)
AppHelper.runEventLoop()
