.PHONY: sim iverilog questa clean

# Parameters (kept in sync with fft-rtl so the FFT project can be reused as-is)
WIDTH ?= 16
INTEGER_WIDTH ?= 2
N ?= 512
ROUND ?= 1
SATURATION ?= 1
RADIX_OUT_SCALE ?= 1

# Testbench selection
TB ?= byte_packer_tb
SIM_TOP = work.$(TB)

RTL_SRCS = $(shell find ./rtl -type f -name "*.sv" ! -name "top.sv")
TB_SRC   = $(shell find ./sim -type f -name "$(TB).sv")

IVERILOG ?= iverilog
VVP      ?= vvp

sim iverilog:
	$(IVERILOG) -g2012 -Wall -o $(TB).vvp $(RTL_SRCS) $(TB_SRC)
	$(VVP) $(TB).vvp

questa:
	vlog +define+WIDTH=$(WIDTH) +define+INTEGER_WIDTH=$(INTEGER_WIDTH) +define+N=$(N) \
	     +define+ROUND=$(ROUND) +define+SATURATION=$(SATURATION) +define+RADIX_OUT_SCALE=$(RADIX_OUT_SCALE) \
	     -work work -sv -f rtl/rtl.f -f sim/sim.f
	vsim -64 -voptargs=+acc $(SIM_TOP)

clean:
	rm -rf work transcript vsim.wlf
	rm -f *.vvp *.vcd
