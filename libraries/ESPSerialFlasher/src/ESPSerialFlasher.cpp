#include "ESPSerialFlasher.h"

#include "serial_io.h"
#include "serial_comm.h"
#include "esp_loader.h"

// Pin / port symbols expected by this library are normally provided by SAMD
// board variant headers (Nano 33 IoT, MKR 1010, RP2040 Connect). On Arduino
// UNO R4 WiFi + Adafruit AirLift Shield they aren't defined, so we map them
// to our wiring here. Override these by -D defines from the sketch if needed.
#ifndef SerialNina
#define SerialNina    Serial1   // R4 D0/D1 -> AirLift -> ESP32 UART0
#endif
#ifndef NINA_RESETN
#define NINA_RESETN   14        // R4 A0 -> ESP32 EN (RST_JMP cut on AirLift)
#endif
#ifndef NINA_GPIO0
#define NINA_GPIO0    6         // R4 D6 -> AirLift G0_JMP -> ESP32 GPIO0
#endif


static int32_t s_time_end;

Print * ESPDebugPort = &Serial;
bool ESPDebug = false;

void ESPFlasherInit( bool _debug, Print * _debugPort ){
SerialNina.begin(115200);
pinMode(NINA_RESETN, OUTPUT);
pinMode(NINA_GPIO0, OUTPUT);
ESPDebug = _debug;
ESPDebugPort = _debugPort;
if(ESPDebug) ESPDebugPort->println("ESP Flasher Init");
}

esp_loader_error_t ESPFlasherConnect(){
	// Sync at 115200 (ESP_LOADER_CONNECT_DEFAULT), then upshift R4<->ESP32
	// link to 921600. Pair with 4KB payload (8KB hangs flash_start).
	return connect_to_target(921600);
}

esp_loader_error_t ESPFlashBin(const char* binFilename){
	if(ESPDebug) ESPDebugPort->println("WARNING! DO NOT INTERRUPT OR WIFI-MODULE WILL BE CORRUPT");
	esp_loader_error_t err = ESP_LOADER_ERROR_FAIL;
	if(SD.exists(binFilename)){
        File UpdateFile = SD.open(binFilename, FILE_READ);
        size_t size = UpdateFile.size();
        if(size <= 0x247000){
            err = flash_binary(UpdateFile,  size,  0x0);
        } else {
            if(ESPDebug) ESPDebugPort->println("File too large for partition");
            err = ESP_LOADER_ERROR_INVALID_PARAM;
        }
        UpdateFile.close();
	} else {
        if(ESPDebug) ESPDebugPort->println("File doesnt exist");
    }
    loader_port_reset_target();
    return err;
}

esp_loader_error_t ESPFlashBinFromStream(Stream &src, size_t size, uint32_t readTimeoutMs){
    if(ESPDebug) ESPDebugPort->println("WARNING! DO NOT INTERRUPT OR WIFI-MODULE WILL BE CORRUPT");
    if(size == 0 || size > 0x247000){
        if(ESPDebug) ESPDebugPort->println("Stream size invalid for partition (must be >0 and <= 0x247000)");
        loader_port_reset_target();
        return ESP_LOADER_ERROR_INVALID_PARAM;
    }
    esp_loader_error_t err = flash_binary_from_stream(src, size, 0x0, readTimeoutMs);
    loader_port_reset_target();
    return err;
}

esp_loader_error_t ESPFlashCert(const char* certFilename){
	if(ESPDebug) ESPDebugPort->println("WARNING! DO NOT INTERRUPT OR WIFI-MODULE WILL BE CORRUPT");
	esp_loader_error_t err = ESP_LOADER_ERROR_FAIL;
    if(SD.exists(certFilename)){
        File CertFile = SD.open(certFilename, FILE_READ);
        size_t size = CertFile.size();
        if(size <= 0x20000){
            err = flash_binary(CertFile,  size,  0x10000);
        } else {
            if(ESPDebug) ESPDebugPort->println("File too large for partition");
            err = ESP_LOADER_ERROR_INVALID_PARAM;
        }
        CertFile.close();
     } else {
        if(ESPDebug) ESPDebugPort->println("File doesnt exist");
     }
     loader_port_reset_target();
     return err;
}

esp_loader_error_t ESPFlashCertFromMemory(const char* Certificates, unsigned long size){
    if(ESPDebug) ESPDebugPort->println("WARNING! DO NOT INTERRUPT OR WIFI-MODULE WILL BE CORRUPT");
    esp_loader_error_t err = ESP_LOADER_ERROR_FAIL;
    if(size <= 0x20000){
        err = flash_binary_from_memory((const uint8_t*) Certificates,  size,  0x10000);
    } else {
        if(ESPDebug) ESPDebugPort->println("File too large for partition");
        err = ESP_LOADER_ERROR_INVALID_PARAM;
    }
    loader_port_reset_target();
    return err;
}

esp_loader_error_t loader_port_serial_write(const uint8_t *data, uint16_t size, uint32_t timeout)
{
    

   size_t err = SerialNina.write((const char *)data, size);

    if (err == size) {
        return ESP_LOADER_SUCCESS;
    } else 
        return ESP_LOADER_ERROR_FAIL;
    
}


esp_loader_error_t loader_port_serial_read(uint8_t *data, uint16_t size, uint32_t timeout)
{
	SerialNina.setTimeout(timeout);
    int read = SerialNina.readBytes( data, size);

    if (read < 0) {
        return ESP_LOADER_ERROR_FAIL;
    } else if (read < size) {
        return ESP_LOADER_ERROR_TIMEOUT;
    } else {
        return ESP_LOADER_SUCCESS;
    }
}


// Set GPIO0 LOW, then
// assert reset pin for 50 milliseconds.
void loader_port_enter_bootloader(void)
{
    digitalWrite(NINA_GPIO0, 0);
    loader_port_reset_target();
    loader_port_delay_ms(50);
    digitalWrite(NINA_GPIO0, 1);
}


void loader_port_reset_target(void)
{
    digitalWrite(NINA_RESETN, 0);
    loader_port_delay_ms(50);
    digitalWrite(NINA_RESETN, 1);
}


void loader_port_delay_ms(uint32_t ms)
{
    delay(ms );
}


void loader_port_start_timer(uint32_t ms)
{
    s_time_end = millis() + ms;
}


uint32_t loader_port_remaining_time(void)
{
    int32_t remaining = (s_time_end - millis()) ;
    return (remaining > 0) ? (uint32_t)remaining : 0;
}


void loader_port_debug_print(const char *str)
{
    if(ESPDebug) ESPDebugPort->print( str);
}

esp_loader_error_t loader_port_change_baudrate(uint32_t baudrate)
{
    SerialNina.begin(baudrate);
    int err = SerialNina;
    return (err == true) ? ESP_LOADER_SUCCESS : ESP_LOADER_ERROR_FAIL;
}

esp_loader_error_t connect_to_target(uint32_t higher_baudrate)
{
 esp_loader_connect_args_t connect_config = ESP_LOADER_CONNECT_DEFAULT();

    esp_loader_error_t err = esp_loader_connect(&connect_config);
    if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) ESPDebugPort->print("Cannot connect to target. Error: %u\n");
        if(ESPDebug) ESPDebugPort->print(err);
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Connected to target\n");
    //if(ESPDebug) ESPDebugPort->println(esp_loader_get_target());

    if (higher_baudrate && esp_loader_get_target() != ESP8266_CHIP) {
        err = esp_loader_change_baudrate(higher_baudrate);
        if (err == ESP_LOADER_ERROR_UNSUPPORTED_FUNC) {
            if(ESPDebug) ESPDebugPort->print("ESP8266 does not support change baudrate command.");
            return err;
        } else if (err != ESP_LOADER_SUCCESS) {
            if(ESPDebug) ESPDebugPort->print("Unable to change baud rate on target.");
            return err;
        } else {
            err = loader_port_change_baudrate(higher_baudrate);
            if (err != ESP_LOADER_SUCCESS) {
                if(ESPDebug) ESPDebugPort->print("Unable to change baud rate.");
                return err;
            }
            printf("Baudrate changed\n");
        }
    }

    return ESP_LOADER_SUCCESS;
}


esp_loader_error_t flash_binary(File file, size_t size, size_t address)
{
		
	
    esp_loader_error_t err;
    uint8_t payload[4096];   // 4KB matches stream variant; max stub-loader-safe

    if(ESPDebug) ESPDebugPort->print("Erasing flash (this may take a while)...\n");
    err = esp_loader_flash_start(address, size, sizeof(payload));
    if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) ESPDebugPort->print("Erasing flash failed with error : ");if(ESPDebug) ESPDebugPort->println(err);
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Start programming\n");
	if(ESPDebug) ESPDebugPort->print("\rProgress: ");
    size_t binary_size = size;
    size_t written = 0;
    int previousProgress = -1;
    while (size > 0) {
        size_t to_read = MIN(size, sizeof(payload));
        file.read(payload,to_read);
        err = esp_loader_flash_write(payload, to_read);
        if (err != ESP_LOADER_SUCCESS) {
            if(ESPDebug) ESPDebugPort->print("\nPacket could not be written! Error : ");
            if(ESPDebug) ESPDebugPort->println( err);
            return err;
        }

        size -= to_read;
        written += to_read;

        int progress = (int)(((float)written / binary_size) * 100);
        if(previousProgress != progress)
        {
		previousProgress = progress;
        if(ESPDebug) ESPDebugPort->print(progress);if(ESPDebug) ESPDebugPort->print(",");
       	}
    };

    if(ESPDebug) ESPDebugPort->print("\nFinished programming\n");

#if MD5_ENABLED
    err = esp_loader_flash_verify();
    if (err == ESP_LOADER_ERROR_UNSUPPORTED_FUNC) {
        if(ESPDebug) ESPDebugPort->print("ESP8266 does not support flash verify command.");
        return err;
    } else if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) ESPDebugPort->print("MD5 does not match. err: %d\n");
        if(ESPDebug) ESPDebugPort->print( err);
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Flash verified\n");
#endif

    return ESP_LOADER_SUCCESS;
}

esp_loader_error_t flash_binary_from_memory(const uint8_t *bin, size_t size, size_t address){
    
		
	
    esp_loader_error_t err;
    uint8_t payload[4096];   // 4KB matches stream variant; max stub-loader-safe
    const uint8_t *bin_addr = bin;
    
    if(ESPDebug) ESPDebugPort->print("Erasing flash (this may take a while)...\n");
    err = esp_loader_flash_start(address, size, sizeof(payload));
    if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) ESPDebugPort->print("Erasing flash failed with error : ");if(ESPDebug) ESPDebugPort->println(err);
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Start programming\n");
   	if(ESPDebug) ESPDebugPort->print("\rProgress: ");
    size_t binary_size = size;
    size_t written = 0;
    int previousProgress = -1;
    while (size > 0) {
        size_t to_read = MIN(size, sizeof(payload));
        memcpy(payload, bin_addr, to_read);
   
  
        err = esp_loader_flash_write(payload, to_read);
        if (err != ESP_LOADER_SUCCESS) {
            if(ESPDebug) ESPDebugPort->print("\nPacket could not be written! Error : ");
            if(ESPDebug) ESPDebugPort->println( err);
            return err;
        }

        size -= to_read;
        bin_addr += to_read;
        written += to_read;

        int progress = (int)(((float)written / binary_size) * 100);
        if(previousProgress != progress)
        {
		previousProgress = progress;
        if(ESPDebug) ESPDebugPort->print(progress);if(ESPDebug) ESPDebugPort->print(",");
       	}
    };

    if(ESPDebug) ESPDebugPort->print("\nFinished programming\n");

#if MD5_ENABLED
    err = esp_loader_flash_verify();
    if (err == ESP_LOADER_ERROR_UNSUPPORTED_FUNC) {
        if(ESPDebug) ESPDebugPort->print("ESP8266 does not support flash verify command.");
        return err;
    } else if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) ESPDebugPort->print("MD5 does not match. err: %d\n");
        if(ESPDebug) ESPDebugPort->print( err);
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Flash verified\n");
#endif

    return ESP_LOADER_SUCCESS;
}

// Block-reads `len` bytes from `src` into `buf`, returns true on success or
// false if `timeoutMs` elapses without enough data. Polls Stream::available().
static bool readExact(Stream &src, uint8_t *buf, size_t len, uint32_t timeoutMs)
{
    size_t got = 0;
    uint32_t deadline = millis() + timeoutMs;
    while (got < len) {
        int n = src.available();
        if (n > 0) {
            int chunk = src.readBytes((char*)(buf + got), min((int)(len - got), n));
            got += chunk;
            deadline = millis() + timeoutMs;  // reset on any progress
        } else {
            if ((int32_t)(millis() - deadline) > 0) return false;
        }
    }
    return true;
}

esp_loader_error_t flash_binary_from_stream(Stream &src, size_t size, size_t address, uint32_t readTimeoutMs)
{
    esp_loader_error_t err;
    // 4096 is the largest chunk the ESP32 stub loader accepts cleanly on
    // this hardware; 8192 caused flash_start to hang.
    uint8_t payload[4096];
    // ACK_WINDOW=1 (per-chunk) is the ceiling on this hardware. Tried 4 and 8
    // — both overran the R4 USB-CDC RX buffer at the first window boundary.
    const int ACK_WINDOW = 1;
    int chunks_in_window = 0;

    if(ESPDebug) ESPDebugPort->print("Erasing flash (this may take a while)...\n");
    err = esp_loader_flash_start(address, size, sizeof(payload));
    if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) { ESPDebugPort->print("Erasing flash failed with error : "); ESPDebugPort->println(err); }
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Start programming\n");
    if(ESPDebug) ESPDebugPort->print("\rProgress: ");

    size_t binary_size = size;
    size_t written = 0;
    int previousProgress = -1;
    while (size > 0) {
        size_t to_read = MIN(size, sizeof(payload));
        if (!readExact(src, payload, to_read, readTimeoutMs)) {
            if(ESPDebug) ESPDebugPort->print("\nERROR: source stream timed out before EOF");
            return ESP_LOADER_ERROR_TIMEOUT;
        }
        err = esp_loader_flash_write(payload, to_read);
        if (err != ESP_LOADER_SUCCESS) {
            if(ESPDebug) { ESPDebugPort->print("\nPacket could not be written! Error : "); ESPDebugPort->println(err); }
            return err;
        }
        // Credit-window ACK: one [ACK] per ACK_WINDOW chunks. Always ACK the
        // final chunk so host knows when streaming is fully complete.
        chunks_in_window++;
        if (chunks_in_window >= ACK_WINDOW || (size - to_read) == 0) {
            ESPDebugPort->print("[ACK]");
            chunks_in_window = 0;
        }
        size -= to_read;
        written += to_read;

        int progress = (int)(((float)written / binary_size) * 100);
        if (previousProgress != progress) {
            previousProgress = progress;
            if(ESPDebug) { ESPDebugPort->print(progress); ESPDebugPort->print(","); }
        }
    }

    if(ESPDebug) ESPDebugPort->print("\nFinished programming\n");

#if MD5_ENABLED
    err = esp_loader_flash_verify();
    if (err == ESP_LOADER_ERROR_UNSUPPORTED_FUNC) {
        if(ESPDebug) ESPDebugPort->print("ESP8266 does not support flash verify command.");
        return err;
    } else if (err != ESP_LOADER_SUCCESS) {
        if(ESPDebug) { ESPDebugPort->print("MD5 does not match. err: %d\n"); ESPDebugPort->print(err); }
        return err;
    }
    if(ESPDebug) ESPDebugPort->print("Flash verified\n");
#endif

    return ESP_LOADER_SUCCESS;
}

