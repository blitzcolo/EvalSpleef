# macos-001：直接 swiftc 构建（无需 SwiftPM / Xcode 工程）
SWIFT ?= swiftc
BUILD := build
SOURCES := Sources/ParticleBench/main.swift Sources/ParticleBench/Shaders.swift Sources/ParticleBench/ParticleRenderer.swift
MODCACHE := $(BUILD)/modulecache
BINARY := $(BUILD)/ParticleBench

.PHONY: all bench clean check

all: $(BINARY)

$(BINARY): $(SOURCES)
	@mkdir -p $(BUILD) $(MODCACHE)
	$(SWIFT) -O -module-cache-path $(MODCACHE) -o $(BINARY) $(SOURCES)

bench: $(BINARY)
	$(BINARY) --frames 30 --particles 24000

check: $(BINARY)
	$(BINARY) --frames 5 --particles 1000 --width 640 --height 360

clean:
	rm -rf $(BUILD)
