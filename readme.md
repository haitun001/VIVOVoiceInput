# VIVO Voice Input

VIVO Voice Input. The default shortcut is **NVDA+Shift+V**: hold to speak and release to stop.

## Before you start

You need a working microphone and a reliable internet connection. You also need a valid NVDACN account. [Click here to visit NVDACN](https://www.nvdacn.com/).

Add-on version: 0.2. Minimum NVDA version: 2026.1. Last tested NVDA version: 2026.1.

## Installation and login

1. Open `VIVOVoiceInput-0.2.nvda-addon`, follow NVDA's installation prompts and restart NVDA.
2. Open the NVDA menu → Preferences → Settings → VIVO Voice Input.
3. Enter your NVDACN username and password, then click "Log in".

After a successful login, your password is saved using Windows DPAPI encryption to protect your account information.

To sign out of your current NVDACN account, click "Log out". This also clears your saved NVDACN account information.

## Choosing a microphone

Choose the microphone you want to use under "Input device" in the add-on settings. "System default input device" uses the default recording device set in Windows.

Click "OK" or "Apply" after making your choice. To keep it for the next time you start NVDA, save the NVDA configuration or enable "Save configuration on exit".

After changing, connecting or disconnecting a microphone, close the entire NVDA settings window and reopen it to refresh the device list.

## Using voice input

1. Place the cursor where you want to enter text.
2. Hold **NVDA+Shift+V** and start speaking after the short, low-pitched beep.
3. When you finish speaking, release the shortcut and any other keys you are holding. A higher-pitched beep tells you that recording has stopped.
4. Wait for the text to be entered before starting another voice input.

Each recording can last up to 60 seconds and stops automatically at the limit. The NVDA key is the key you have configured for NVDA, such as Insert or Caps Lock.

Stay in the same input location while speaking and waiting for the result. Moving elsewhere cancels that voice input.

## Changing the shortcut

To choose a shortcut that suits you better, open the NVDA menu → Preferences → Input gestures. Under "VIVO Voice Input", find "Hold for speech recognition; release to stop recognition" and change the shortcut.

## Tips

- If the selected microphone is unavailable when NVDA starts, the add-on will try the system default microphone and announce a message. If you still cannot record, check the microphone connection and Windows recording settings.
- If something goes wrong, NVDA will read out a message. Follow the message and try again.
- Some programs or input locations may not accept the text. You can try another input location.
- Recordings are sent to VIVO's online service for recognition.
