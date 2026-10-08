// syscap: capture all audio the Mac is playing (Zoom, Meet, Teams, browser, ...) with
// ScreenCaptureKit and write it to stdout as raw little-endian Float32, 16 kHz, mono.
// Murmur's own process audio is excluded. Needs "Screen & System Audio Recording" permission
// for the app that launches it (your terminal).
//
// Build: swiftc -O -o native/syscap native/syscap.swift
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

setvbuf(stdout, nil, _IONBF, 0)
signal(SIGPIPE, SIG_IGN)

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    var stream: SCStream?
    let out = FileHandle.standardOutput
    let queue = DispatchQueue(label: "syscap.audio")

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw NSError(domain: "syscap", code: 1) }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = 16000
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        // we only want audio: keep the video side as cheap as possible
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 3
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await s.startCapture()
        stream = s
        log("syscap: capturing system audio")
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid else { return }
        var abl = AudioBufferList()
        var block: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: &abl,
            bufferListSize: MemoryLayout<AudioBufferList>.size, blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil, flags: 0, blockBufferOut: &block)
        guard st == noErr else { return }
        let buffers = UnsafeMutableAudioBufferListPointer(&abl)
        guard let first = buffers.first, let data = first.mData else { return }
        if buffers.count == 1 {
            out.write(Data(bytes: data, count: Int(first.mDataByteSize)))
        } else {
            // non-interleaved multi-channel: average to mono
            let n = Int(first.mDataByteSize) / 4
            var mono = [Float](repeating: 0, count: n)
            for b in buffers {
                guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
                for i in 0..<n { mono[i] += p[i] / Float(buffers.count) }
            }
            mono.withUnsafeBytes { out.write(Data($0)) }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("syscap: stopped: \(error.localizedDescription)")
        exit(2)
    }
}

let cap = Capture()
Task {
    do { try await cap.start() } catch {
        log("syscap: failed to start: \(error.localizedDescription)")
        log("syscap: grant Screen & System Audio Recording permission to your terminal in System Settings > Privacy & Security")
        exit(1)
    }
}
// exit when the parent closes our stdin (server stopped recording)
DispatchQueue.global().async {
    while FileHandle.standardInput.availableData.count > 0 {}
    exit(0)
}
RunLoop.main.run()
