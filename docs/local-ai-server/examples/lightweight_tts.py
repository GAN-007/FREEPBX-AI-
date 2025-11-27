#!/usr/bin/env python3
"""
Lightweight TTS wrapper using espeak-ng
Compatible with older CPU architectures that don't support NNPACK
"""

import subprocess
import tempfile
import os
import logging

logger = logging.getLogger(__name__)

class LightweightTTS:
    """Lightweight TTS using espeak-ng system command"""
    
    def __init__(self):
        self.voice = "en"  # English voice
        self.speed = 150   # Speech speed (words per minute)
        self.pitch = 50    # Voice pitch (0-99)
        self.volume = 100  # Volume (0-200)
        
    def tts(self, text: str) -> bytes:
        """
        Convert text to speech using espeak-ng with direct ulaw output for Asterisk compatibility
        
        Args:
            text: Text to convert to speech
            
        Returns:
            bytes: ulaw audio data at 8000 Hz sample rate
        """
        try:
            # Create temporary file for output
            with tempfile.NamedTemporaryFile(suffix='.ulaw', delete=False) as temp_file:
                temp_path = temp_file.name
            
            # Use espeak-ng to generate WAV file, then convert to ulaw with sox
            espeak_temp = temp_path.replace('.ulaw', '_espeak.wav')
            
            espeak_cmd = [
                'espeak-ng',
                '-v', self.voice,           # Voice
                '-s', str(self.speed),      # Speed
                '-p', str(self.pitch),      # Pitch
                '-a', str(self.volume),     # Volume
                '-w', espeak_temp,          # Output to temporary file
                text                        # Text to speak
            ]
            
            sox_cmd = [
                'sox',
                espeak_temp,                # Input file from espeak-ng
                '-r', '8000',               # Sample rate: 8000 Hz
                '-c', '1',                  # Mono channel
                '-e', 'mu-law',             # mu-law encoding
                '-t', 'raw',                # Raw format
                temp_path                   # Output as ulaw file
            ]
            
            logger.debug(f"Running espeak-ng + sox pipeline for ulaw output at 8000 Hz")
            
            # Execute espeak-ng first
            espeak_result = subprocess.run(espeak_cmd, capture_output=True, text=True, timeout=30)
            
            if espeak_result.returncode != 0:
                logger.error(f"espeak-ng failed: {espeak_result.stderr}")
                return b""
            
            # Then convert with sox
            sox_result = subprocess.run(sox_cmd, capture_output=True, text=True, timeout=10)
            
            if sox_result.returncode != 0:
                logger.error(f"sox conversion failed: {sox_result.stderr}")
                return b""
            
            # Clean up intermediate file
            if os.path.exists(espeak_temp):
                os.unlink(espeak_temp)
            
            # Read the generated ulaw file
            if os.path.exists(temp_path):
                with open(temp_path, 'rb') as f:
                    audio_data = f.read()
                
                # Clean up temporary file
                os.unlink(temp_path)
                
                logger.debug(f"Generated TTS audio as ulaw at 8000 Hz: {len(audio_data)} bytes")
                return audio_data
            else:
                logger.error("TTS pipeline did not generate output file")
                return b""
                
        except subprocess.TimeoutExpired:
            logger.error("TTS pipeline timed out")
            return b""
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return b""
    
    def set_voice(self, voice: str):
        """Set the voice (e.g., 'en', 'en-us', 'en-gb')"""
        self.voice = voice
    
    def set_speed(self, speed: int):
        """Set speech speed (words per minute, 80-500)"""
        self.speed = max(80, min(500, speed))
    
    def set_pitch(self, pitch: int):
        """Set voice pitch (0-99)"""
        self.pitch = max(0, min(99, pitch))
    
    def set_volume(self, volume: int):
        """Set volume (0-200)"""
        self.volume = max(0, min(200, volume))

# Test function
def test_lightweight_tts():
    """Test the lightweight TTS system"""
    print("Testing Lightweight TTS...")
    
    tts = LightweightTTS()
    
    # Test basic TTS
    test_text = "Hello, this is a test of the lightweight text to speech system."
    audio_data = tts.tts(test_text)
    
    if audio_data:
        print(f"✅ TTS generation successful: {len(audio_data)} bytes")
        
        # Test different settings
        tts.set_speed(120)  # Slower speech
        tts.set_pitch(60)   # Higher pitch
        
        audio_data2 = tts.tts("This is a slower, higher pitched voice.")
        if audio_data2:
            print(f"✅ Voice customization successful: {len(audio_data2)} bytes")
        else:
            print("❌ Voice customization failed")
    else:
        print("❌ TTS generation failed")
    
    return len(audio_data) > 0

if __name__ == "__main__":
    test_lightweight_tts()
