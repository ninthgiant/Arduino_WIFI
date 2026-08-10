#ifndef ESP_FLASHER_H
#define ESP_FLASHER_H

#include "serial_io.h"

#include "Arduino.h"
#include "SD.h"



extern Print *ESPDebugPort; 
extern bool _ESPDebug;


void ESPFlasherInit(bool _debug = false, Print *_debugPort = &Serial );
// All ESPFlash* entry points return ESP_LOADER_SUCCESS on success or an
// esp_loader_error_t code on failure. Callers should NOT reset / declare
// success without checking — e.g. an MD5 mismatch will surface here.
esp_loader_error_t ESPFlasherConnect();
esp_loader_error_t ESPFlashBin(const char* binFilename);
esp_loader_error_t ESPFlashBinFromStream(Stream &src, size_t size, uint32_t readTimeoutMs = 10000);
esp_loader_error_t ESPFlashCert(const char* certFilename);
esp_loader_error_t ESPFlashCertFromMemory(const char* Certificates, unsigned long size);

esp_loader_error_t connect_to_target(uint32_t higher_baudrate);
esp_loader_error_t flash_binary(File file, size_t size, size_t address);
esp_loader_error_t flash_binary_from_memory(const uint8_t *bin, size_t size, size_t address);
esp_loader_error_t flash_binary_from_stream(Stream &src, size_t size, size_t address, uint32_t readTimeoutMs);


#endif
