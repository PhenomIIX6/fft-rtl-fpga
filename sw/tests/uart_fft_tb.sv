`timescale 1ns/1ps
// Byte-level host fixture. Parameters and fixture files are supplied by test_rtl.py.
module uart_fft_tb;
    parameter N = 16;
    parameter WIDTH = 16;
    parameter INTEGER_WIDTH = 2;
    parameter ROUND = 1;
    parameter SATURATION = 1;
    parameter RADIX_OUT_SCALE = 1;
    parameter FRAMES = 2;
    localparam PRESCALE = 4;
    localparam BIT_CYCLES = 8 * PRESCALE;
    localparam FRAME_BYTES = N * ((2 * WIDTH + 7) / 8);

    reg clk = 0;
    always #10 clk = ~clk;
    reg arst_n = 0;
    reg rxd = 1;
    wire txd;
    reg [7:0] input_bytes [0:FRAMES*FRAME_BYTES-1];
    reg [7:0] received;
    integer output_file;
    integer received_count = 0;
    integer frame_index;
    integer byte_index;
    integer bit_index;

    top #(
        .N(N), .WIDTH(WIDTH), .INTEGER_WIDTH(INTEGER_WIDTH),
        .ROUND(ROUND), .SATURATION(SATURATION), .RADIX_OUT_SCALE(RADIX_OUT_SCALE),
        .UART_PRESCALE(PRESCALE)
    ) dut (.clk(clk), .arst_n(arst_n), .rxd(rxd), .txd(txd));

    task send_byte(input reg [7:0] data);
        integer bit_number;
        begin
            @(negedge clk);
            rxd = 0;
            repeat(BIT_CYCLES) @(negedge clk);
            for (bit_number = 0; bit_number < 8; bit_number = bit_number + 1) begin
                rxd = data[bit_number];
                repeat(BIT_CYCLES) @(negedge clk);
            end
            rxd = 1;
            repeat(BIT_CYCLES) @(negedge clk);
        end
    endtask

    initial begin
        $readmemh("input.hex", input_bytes);
        output_file = $fopen("output.hex", "w");
        if (!output_file) $fatal(1, "cannot open output.hex");
        repeat(5) @(negedge clk);
        arst_n = 1;
        repeat(5) @(negedge clk);
        for (frame_index = 0; frame_index < FRAMES; frame_index = frame_index + 1) begin
            for (byte_index = 0; byte_index < FRAME_BYTES; byte_index = byte_index + 1)
                send_byte(input_bytes[frame_index * FRAME_BYTES + byte_index]);
            wait(received_count == (frame_index + 1) * FRAME_BYTES);
            repeat(2 * BIT_CYCLES) @(negedge clk);
        end
        $fclose(output_file);
        $display("UART_FRAMES_DONE %0d", received_count);
        $finish;
    end

    initial begin
        wait(arst_n);
        forever begin
            @(negedge txd);
            repeat(BIT_CYCLES + BIT_CYCLES/2) @(posedge clk);
            for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1) begin
                received[bit_index] = txd;
                repeat(BIT_CYCLES) @(posedge clk);
            end
            if (txd !== 1'b1) $fatal(1, "bad UART stop bit");
            $fdisplay(output_file, "%02x", received);
            received_count = received_count + 1;
        end
    end

    initial begin
        repeat(FRAMES * FRAME_BYTES * BIT_CYCLES * 40 + 100000) @(posedge clk);
        $fatal(1, "UART simulation timeout; received %0d bytes", received_count);
    end
endmodule
