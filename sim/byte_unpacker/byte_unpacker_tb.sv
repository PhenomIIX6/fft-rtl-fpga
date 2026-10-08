`timescale 1ns / 1ps

// Self-checking testbench for byte_unpacker.
//
// Drives random DATA_WIDTH words with random valid/ready handshakes and checks:
//   * a word is accepted only when the module is idle (high s_axis_tready);
//   * the module returns to idle only after all BYTES_COUNT bytes are emitted;
//   * emitted bytes reproduce the word least-significant byte first;
//   * no byte is emitted before a word was accepted.
module byte_unpacker_tb;
    localparam int DATA_WIDTH  = 32;
    localparam int BYTES_COUNT = DATA_WIDTH / 8;
    localparam int CLK_PERIOD  = 20;
    localparam int N_WORDS     = 1000;

    logic clk;
    logic arst_n;

    logic [DATA_WIDTH-1:0] s_axis_tdata;
    logic                  s_axis_tvalid;
    logic                  s_axis_tready;
    logic [7:0]            m_axis_tdata;
    logic                  m_axis_tvalid;
    logic                  m_axis_tready;

    byte_unpacker #(
        .DATA_WIDTH(DATA_WIDTH)
    ) dut (
        .clk           (clk),
        .arst_n        (arst_n),

        .s_axis_tdata  (s_axis_tdata),
        .s_axis_tready (s_axis_tready),
        .s_axis_tvalid (s_axis_tvalid),

        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tready (m_axis_tready),
        .m_axis_tvalid (m_axis_tvalid)
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
    byte expected[$];   // bytes that must appear on the output, in order

    task automatic fail(input string msg);
        errors++;
        if (errors <= 20)
            $display("[%t] FAIL: %s", $time, msg);
    endtask

    initial begin
        s_axis_tdata  = '0;
        s_axis_tvalid = 1'b0;
        m_axis_tready = 1'b1;

        @(posedge arst_n);
        @(negedge clk);

        // idle after reset: ready for a word, no output
        if (m_axis_tvalid !== 1'b0)
            fail("m_axis_tvalid must be low right after reset");
        if (s_axis_tready !== 1'b1)
            fail("s_axis_tready must be high right after reset (module is idle)");

        for (int i = 0; i < N_WORDS; i = i + 1) begin
            s_axis_tvalid = ($urandom_range(0, 3) != 0);
            s_axis_tdata  = $urandom;
            m_axis_tready = ($urandom_range(0, 3) != 0);

            @(posedge clk);

            // input handshake
            if (s_axis_tvalid && s_axis_tready) begin
                if (expected.size() != 0)
                    fail("accepted a new word while previous bytes were still pending");
                for (int b = 0; b < BYTES_COUNT; b = b + 1)
                    expected.push_back(s_axis_tdata[b*8 +: 8]);
            end

            // output handshake
            if (m_axis_tvalid && m_axis_tready) begin
                if (expected.size() == 0)
                    fail("emitted a byte without an accepted input word");
                else begin
                    checks++;
                    if (m_axis_tdata !== expected[0])
                        fail($sformatf("unpacked byte mismatch: got 0x%02h, expected 0x%02h",
                                       m_axis_tdata, expected[0]));
                    void'(expected.pop_front());
                end
            end

            @(negedge clk);
        end

        // drain remaining bytes of the last word
        s_axis_tvalid = 1'b0;
        m_axis_tready = 1'b1;
        for (int i = 0; i < BYTES_COUNT + 4 && expected.size() != 0; i = i + 1) begin
            @(posedge clk);
            if (m_axis_tvalid && m_axis_tready) begin
                if (expected.size() == 0)
                    fail("emitted a byte without an accepted input word");
                else begin
                    checks++;
                    if (m_axis_tdata !== expected[0])
                        fail($sformatf("unpacked byte mismatch: got 0x%02h, expected 0x%02h",
                                       m_axis_tdata, expected[0]));
                    void'(expected.pop_front());
                end
            end
            @(negedge clk);
        end

        if (expected.size() != 0)
            fail($sformatf("%0d accepted byte(s) never appeared on the output", expected.size()));

        if (errors == 0) begin
            $display("[%t] byte_unpacker_tb PASS (%0d byte(s) checked)", $time, checks);
            $finish;
        end
        else begin
            $display("[%t] byte_unpacker_tb FAILED with %0d error(s)", $time, errors);
            $fatal(1, "byte_unpacker_tb failed");
        end
    end
endmodule
