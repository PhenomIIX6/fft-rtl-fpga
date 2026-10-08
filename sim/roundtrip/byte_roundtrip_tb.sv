`timescale 1ns / 1ps

// Integration (round-trip) testbench: byte_packer -> byte_unpacker.
//
// A random byte stream is pushed into the packer; the packed words are fed
// straight into the unpacker (the unpacker's backpressure also stalls the
// packer). The byte stream coming out of the unpacker must reproduce the
// input stream exactly and in order.
module byte_roundtrip_tb;
    localparam int DATA_WIDTH  = 32;
    localparam int BYTES_COUNT = DATA_WIDTH / 8;
    localparam int CLK_PERIOD  = 20;
    localparam int N_BYTES     = BYTES_COUNT * 250;   // whole number of words

    logic clk;
    logic arst_n;

    logic [7:0]            src_tdata;
    logic                  src_tvalid;
    logic                  src_tready;

    logic [DATA_WIDTH-1:0] word_tdata;
    logic                  word_tvalid;
    logic                  word_tready;

    logic [7:0]            out_tdata;
    logic                  out_tvalid;
    logic                  out_tready;

    byte_packer #(
        .DATA_WIDTH(DATA_WIDTH)
    ) packer (
        .clk           (clk),
        .arst_n        (arst_n),
        .s_axis_tdata  (src_tdata),
        .s_axis_tready (src_tready),
        .s_axis_tvalid (src_tvalid),
        .m_axis_tdata  (word_tdata),
        .m_axis_tvalid (word_tvalid),
        .m_axis_tready (word_tready)
    );

    byte_unpacker #(
        .DATA_WIDTH(DATA_WIDTH)
    ) unpacker (
        .clk           (clk),
        .arst_n        (arst_n),
        .s_axis_tdata  (word_tdata),
        .s_axis_tready (word_tready),
        .s_axis_tvalid (word_tvalid),
        .m_axis_tdata  (out_tdata),
        .m_axis_tvalid (out_tvalid),
        .m_axis_tready (out_tready)
    );

    initial begin
        clk = 1'b0;
        forever begin
            clk = ~clk;
            #(CLK_PERIOD / 2);
        end
    end

    initial begin
        arst_n = 1'b0;
        repeat (4) @(posedge clk);
        arst_n = 1'b1;
    end

    int  errors = 0;
    int  checks = 0;
    byte expected[$];   // bytes accepted at the source, still awaited at the sink

    task automatic fail(input string msg);
        errors++;
        if (errors <= 20)
            $display("[%t] FAIL: %s", $time, msg);
    endtask

    task automatic check_sink();
        if (out_tvalid && out_tready) begin
            if (expected.size() == 0) begin
                fail("round-trip produced a byte before any input byte was accepted");
            end
            else begin
                checks++;
                if (out_tdata !== expected[0])
                    fail($sformatf("round-trip byte mismatch: got 0x%02h, expected 0x%02h",
                                   out_tdata, expected[0]));
                void'(expected.pop_front());
            end
        end
    endtask

    initial begin
        src_tvalid = 1'b0;
        src_tdata  = '0;
        out_tready = 1'b1;

        @(posedge arst_n);
        @(negedge clk);

        // push N_BYTES bytes; the source always has data (input stream never stops)
        for (int i = 0; i < N_BYTES; i = i + 1) begin
            src_tvalid = 1'b1;
            src_tdata  = $urandom;
            out_tready = ($urandom_range(0, 3) != 0);

            @(posedge clk);

            if (src_tvalid && src_tready)
                expected.push_back(src_tdata);

            check_sink();

            @(negedge clk);
        end

        // stop feeding and drain everything still in flight
        src_tvalid = 1'b0;
        out_tready = 1'b1;
        for (int i = 0; i < N_BYTES && expected.size() != 0; i = i + 1) begin
            @(posedge clk);
            check_sink();
            @(negedge clk);
        end

        if (expected.size() != 0)
            fail($sformatf("%0d byte(s) never came back through the round-trip", expected.size()));

        if (errors == 0) begin
            $display("[%t] byte_roundtrip_tb PASS (%0d byte(s) checked, %0d word(s))",
                     $time, checks, N_BYTES / BYTES_COUNT);
            $finish;
        end
        else begin
            $display("[%t] byte_roundtrip_tb FAILED with %0d error(s)", $time, errors);
            $fatal(1, "byte_roundtrip_tb failed");
        end
    end
endmodule
