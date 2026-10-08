`timescale 1ns / 1ps

// Self-checking testbench for byte_packer.
//
// Drives a random byte stream with random valid/ready handshakes and checks:
//   * exactly BYTES_COUNT bytes are packed into one DATA_WIDTH word;
//   * the first received byte lands in the least significant byte (little endian);
//   * the source is stalled while a complete word is waiting to be consumed;
//   * no word is emitted before BYTES_COUNT bytes were accepted.
module byte_packer_tb;
    localparam int DATA_WIDTH  = 32;
    localparam int BYTES_COUNT = DATA_WIDTH / 8;
    localparam int CLK_PERIOD  = 20;
    localparam int N_BYTES     = 2000;

    logic clk;
    logic arst_n;

    logic [7:0]            s_axis_tdata;
    logic                  s_axis_tvalid;
    logic                  s_axis_tready;
    logic [DATA_WIDTH-1:0] m_axis_tdata;
    logic                  m_axis_tvalid;
    logic                  m_axis_tready;

    byte_packer #(
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
    byte pending[$];   // accepted bytes of the word currently being assembled

    task automatic fail(input string msg);
        errors++;
        if (errors <= 20)
            $display("[%t] FAIL: %s", $time, msg);
    endtask

    task automatic check_output();
        logic [DATA_WIDTH-1:0] expected;
        if (pending.size() != BYTES_COUNT) begin
            fail($sformatf("emitted a word while %0d byte(s) pending (expected %0d)",
                           pending.size(), BYTES_COUNT));
        end
        else begin
            expected = '0;
            for (int b = 0; b < BYTES_COUNT; b = b + 1)
                expected[b*8 +: 8] = pending[b];
            checks++;
            if (m_axis_tdata !== expected)
                fail($sformatf("packed word mismatch: got 0x%h, expected 0x%h",
                               m_axis_tdata, expected));
            for (int b = 0; b < BYTES_COUNT; b = b + 1)
                void'(pending.pop_front());
        end
    endtask

    initial begin
        s_axis_tdata  = '0;
        s_axis_tvalid = 1'b0;
        m_axis_tready = 1'b1;

        @(posedge arst_n);
        @(negedge clk);

        if (m_axis_tvalid !== 1'b0)
            fail("m_axis_tvalid must be low right after reset");
        if (s_axis_tready !== 1'b1)
            fail("s_axis_tready must be high right after reset");

        // random byte stream with random backpressure
        for (int i = 0; i < N_BYTES; i = i + 1) begin
            s_axis_tvalid = ($urandom_range(0, 3) != 0);
            s_axis_tdata  = $urandom;
            m_axis_tready = ($urandom_range(0, 3) != 0);

            @(posedge clk);

            if (s_axis_tvalid && s_axis_tready) begin
                if (pending.size() >= BYTES_COUNT)
                    fail("accepted a byte while a complete word was still pending");
                pending.push_back(s_axis_tdata);
            end

            if (m_axis_tvalid && m_axis_tready)
                check_output();

            @(negedge clk);
        end

        // drain: flush a complete word if one is pending
        s_axis_tvalid = 1'b0;
        m_axis_tready = 1'b1;
        for (int i = 0; i < 4 && pending.size() == BYTES_COUNT; i = i + 1) begin
            @(posedge clk);
            if (m_axis_tvalid && m_axis_tready)
                check_output();
            @(negedge clk);
        end

        if (errors == 0) begin
            $display("[%t] byte_packer_tb PASS (%0d word(s) checked)", $time, checks);
            $finish;
        end
        else begin
            $display("[%t] byte_packer_tb FAILED with %0d error(s)", $time, errors);
            $fatal(1, "byte_packer_tb failed");
        end
    end
endmodule
