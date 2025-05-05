---
Date: 2023-12-28
---

# Project Ignition

This project came about because Twitch generously gives us two weeks off between Christmas and New Years. It's an amazing perk where I get to stop actively thinking about work for a bit (of course I'm checking Slack here and there to make sure nothing blew up and turning over some ideas for 2024 in the back of my head), but that doesn't stop me from wanting to do... *something*. I wasn't quite sure what though, but I knew I wanted to program something for fun; something that had nothing to do with developing my career or developing skills for future monetary gain, just fun like how I got into programming. "*I want to make something stupid*" as I told my girlfriend.

Then I went to smoke a tri-tip in my [Kamado Joe Classic 3](https://www.kamadojoe.com/products/classic-joe-iii) and realized that my meat thermometer couldn't hold a charge no matter how long it was plugged in. I probably could have spent more time figuring this out, but I knew that there was this thermometer from this place [Combustion Inc](https://combustion.inc/) and J. Kenji Lopez-Alt recommended it.

Apparently it has **8** sensors and can predict the **true core temperature** of your meat. That's all well and cool, but after browsing the page I noticed that they have documentation for developers and an open spec for their Bluetooth implementation. That's when I came up with my idea: I bought a [Raspberry Pi 4](https://www.adafruit.com/product/4296) because the 5 was out of stock, a [tiny screen](https://www.adafruit.com/product/4484), and a [USB microphone](https://www.adafruit.com/product/3367) and patiently waited for them all to arrive. I'd make... a thing. A display of sorts, maybe more.

# Rust-bustion

That's what started [Rustbustion](https://github.com/benhirsch24/rustbustion), a stupid program to talk from a Raspberry Pi to a bluetooth thermometer. I decided to use Rust because I like the language. I've followed it for a few years and even got to ship a production feature (well it's planning to GA in Q1 2024) using it and CGo and it's an enjoyable systems language. I might not build an entire company on Rust, but it's fun to use.

## Basic Connection

My first goal was just to talk to the dang thing and figure out how this whole Bluetooth deal worked.

I started with using a library called [BlueR](https://docs.rs/bluer/latest/bluer/) which seems to be official Rust bindings to [BlueZ](http://www.bluez.org/) which is the Linux stack's implementation of Bluetooth. From what I've gathered programs use BlueZ by interfacing with [DBus](https://www.freedesktop.org/wiki/Software/dbus/) which is a message bus used for inter-process communication. Programs send a message with a namespace to DBus which routes it to the correct service and listens for the answer.

There was some basic setup to [update the Raspberry Pi bluetooth stack and install some dependencies](https://scribles.net/updating-bluez-on-raspberry-pi-from-5-43-to-5-50/) but once I got that worked out I was able to use the [BlueR client example](https://github.com/bluez/bluer/blob/e7b745d6c7d4447641b5bdefa0879274b051939f/bluer/examples/gatt_client.rs#L161) to start seeing what devices were out there chatting.

From the [Combustion documentation](https://github.com/combustion-inc/combustion-documentation/blob/main/probe_ble_specification.rst#legacy-ble-4-0-advertisement-packet) I saw that the first two bytes of the manufacturer data in the advertisement packet were `0x09C7` so it was easy enough to [look for that](https://github.com/benhirsch24/rustbustion/blob/01d6e7f687d02ecd485adf2aa8c9eb215cb70c2f/src/main.rs#L140). I could scan the ether for Bluetooth advertisements, find my thermometer, connect to it, and disconnect. Ready to make a billion bucks!

### Joys of Hobby Programming

As an aside I went to my girlfriend's family's home for Christmas and apparently left the thermometer out the entire time, so when I came back it was out of battery. After charging it for a few hours I could no longer discover it from my program. Wtf!

I ended up using `bluetoothctl` to debug this. The rough steps to deal with this (from memory) were to:

1. `sudo bluetoothd` in one terminal to run the bluetooth daemon
2. In another terminal, `bluetoothctl` and enter into the CLI app
3. `devices` to list the known devices. `disconnect <address>` and `remove <address>` to remove it from the list of known addresses to the Pi.

At this point it still didn't show up. After fidgeting with it by docking and removing it repeatedly it did show back up. I'm unsure if these steps were necessary but learning a bit about `bluetoothctl` would prove to be useful down the line.

## Milestone 2: how hot is it?

Having achieved the monumental task of discovering and connecting to the thermometer it was time to read some data from it. My bluetooth knowledge journey commenced.

A Bluetooth device can expose one or more Services each with their own unique UUID the manufacturer tells you about. Each service can have one or more Characteristics again with their own UUIDs.

To read the probe temperature (this is the probe at the very tip of the thermometer) I would [find the Service](https://github.com/benhirsch24/rustbustion/blob/main/src/main.rs#L108) with UUID `00000100-CAAB-3792-3D44-97AE51C1407A`

```
    let mut probe_service: Option<Service> = None;
    let mut uart_service: Option<Service> = None;

    for service in device.services().await? {
        let uuid = service.uuid().await?;
        info!("  Service UUID: {} ID: {}", &uuid, service.id());
        if uuid == probe_uuid {
            probe_service.replace(service.clone());
        } else if uuid == uart_uuid {
            uart_service.replace(service.clone());
        }
        info!("  Service data: {:?}", service.all_properties().await?);
    }
```

List the Characteristics (there is only one), interrogate it to make sure it is "readable", and read it which ends up returning 48 bytes. [Code Permalink](https://github.com/benhirsch24/rustbustion/blob/01d6e7f687d02ecd485adf2aa8c9eb215cb70c2f/src/main.rs#L45C1-L56C1)

```
    for c in combustion.probe_status_service.characteristics().await? {
        let uuid = c.uuid().await?;
        info!("Probe Status c: {}", &uuid);
        let mut interval = tokio::time::interval(tokio::time::Duration::from_millis(2000));
        loop {
            tokio::select! {
                _ = interval.tick() => {
                    // If the characteristic flags includes "read"
                    if c.flags().await?.read {
                        // Read it
                        let value = c.read().await?;

```

### Next Great Adventure

At this point I have a blob of data. [The docs seem pretty straightforward as to what I got](https://github.com/combustion-inc/combustion-documentation/blob/main/probe_ble_specification.rst#probe-status-service). Two u32 integers that tell me how many records there are

```
                        let min_bytes: [u8; 4] = [value[0], value[1], value[2], value[3]];
                        let max_bytes: [u8; 4] = [value[4], value[5], value[6], value[7]];
                        let min = u32::from_ne_bytes(min_bytes);
                        let max = u32::from_ne_bytes(max_bytes);
                        info!("Min {} max {}", min, max);
```

and then [13 bytes with the 8 temperatures](https://github.com/combustion-inc/combustion-documentation/blob/main/probe_ble_specification.rst#raw-temperature-data), 13 bits each. Celsius is then calculated from this 13 bit number as `C = 0.05 * N - 20`.

Unfortunately this took me a while to figure out to my own chagrin. Reader, I've done my fair share of bit-twiddling. My first job out of college was programming GPUs and warping images so I've written a PPM parser and writer many many times. I quantized floats into u8s to speed up GPU kernels before LLMs were a thing.

Somehow though it took me hours to figure this thing out.

One of the things that really helped was using `bluetoothctl` to play around with the thermometer a bit faster. I could `connect <address>` to the device, `menu gatt` to get to the remote services menu, `list-attributes` to list all the attributes, `select-attribute <attribute>`, and finally `read` to read it.

From there I'd `read` the attribute over and over while warming the probe with my fingers. I'd watch bytes 8 & 9 tick up from `0xDF 0x03` to `0xFF 0x03` and back down and could not figure out to arrange the first 13 bits just so.

Eventually I wrote an [easy C program](https://github.com/benhirsch24/rustbustion/blob/main/extra/repr.c) to play with as I found a [Reddit thread where an employee gave some C code](https://old.reddit.com/r/combustion_inc/comments/16wvfv3/interpreting_raw_temperature_data/k3cr0fh/) for the packed struct. After playing around with it I finally understood how the bits are laid out:

For a 13 bit number you would need two bytes: `[a8a7a6a5a4a3a2a1, 000a13a12a11a10a9]`. Adding a second 13-bit number would require 4 bytes: `[a8a7a6a5a4a3a2a1, b3b2b1a13a12a11a10a9, b11b10b9b8b7b6b5b4, 000000b13b12]`

Now that I understand this it's so hard to put myself back in my original headspace to figure out how I spent close to 4 hours on this. But I did, oh well. If I couldn't spend 4 hours to understand something simple and then laugh it off afterwards I wouldn't last in this career.

I found a Rust library called [Modular Bitfield](docs.rs/modular_bitfield) where I could [easily define my packed struct](https://github.com/benhirsch24/rustbustion/blob/01d6e7f687d02ecd485adf2aa8c9eb215cb70c2f/src/main.rs#L30)

```
struct RawTempData {
    t1: B13,
    t2: B13,
    t3: B13,
    ...
}
```

And then

```
                        let vs: [u8; 13] = value[8..21].try_into().expect("13");
                        let unpacked = RawTempData::from_bytes(vs);
                        let t1c = (unpacked.t1() * 5 - 2000) as f32 / 100.0;
```

And we were cooking! Metaphorically, I haven't yet cooked with the thermometer.

### Final touches

I added a simple server using [Hyper](https://hyper.rs) to expose the last read temperature value as a local server so I could `curl http://127.0.0.1:3000` to get that numerical value. I also spawned a task to listen for CtrlC using `tokio::signal::ctrl_c()` to gracefully shut down the program and that was that. A good first "stupid fun" Bluetooth program.

## Display

I had one more mission and that was to display something on my tiny tiny touch screen.

It's pretty basic, but I started with the Adafruit example code and [ended up with this](https://github.com/benhirsch24/rustbustion/blob/01d6e7f687d02ecd485adf2aa8c9eb215cb70c2f/display.py#L49C1-L62C1):

```
    while True:
        draw.rectangle((0, 80, 240, 160), outline=0, fill=(0, 255, 0))
        f = urllib.request.urlopen("http://127.0.0.1:3000")
        temp = f.read().decode("utf-8")
        draw.text((x, y), "Temp: " + temp, font=font, fill="#FFFFFF")
        coords = "X: " + str(x) + " Y: " + str(y)
        draw.text((0, 0), coords, font=font, fill="#FFFFFF")
        display.image(image, 180)
        draw.rectangle((0, 0, 240, 320), outline=0, fill=0)
        if buttonA.value and not buttonB.value:
            y += 5
        if not buttonA.value and buttonB.value:
            y -= 5
```

Each loop we pull the latest temperature from the HTTP server and write it to the screen. The buttons move the text up and down. To learn about the coordinate system I display the current coordinates of the temperature in the upper left. Then we wipe and repeat in a tight loop.

Simple, stupid, fun.

# What's Next

Here's a brainstormed list of ideas for what to do next:

1. Pull more data off the probe. It looks like there are [temperature logs](https://github.com/combustion-inc/combustion-documentation/blob/main/probe_ble_specification.rst#read-logs-0x04) so I could display graphs or show the last N temperatures. There's also various prediction modes.
    1. From here I could page through different options with the display.
2. Send data to the cloud.
    1. Simple range extender for when I'm out and about and not in BT range.
    2. Send and store log data per item cooked
    3. Create a "Hey what's cookin" website where I create a unique link per cook to send to friends for watching. That way they know exactly when to show up for when it'll be done.
3. Display little images on the Pi display corresponding to the item cooking
    1. This one does relate to "career" things a little, but I'd like to implement a simple Stable Diffusion model to learn how that works. Depending how deep the rabbit hole goes and how much time I have I could optimize it for the little Pi GPU.
4. "Productionize" it by starting the Rust and Display programs at startup so I can simply plug the Pi in downstairs near my WiFi and the probe and have it hook up immediately to the thermometer rather than having to use mouse/keyboard/display
