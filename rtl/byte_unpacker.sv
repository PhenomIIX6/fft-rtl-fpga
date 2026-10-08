module byte_unpacker #(
    parameter DATA_WIDTH = 32,

    parameter BYTES_COUNT = int'($ceil(DATA_WIDTH / 8)),
    parameter ADDRSIZE = $clog2(BYTES_COUNT+1)
) (
    input  logic clk,
    input  logic arst_n,

    input  logic [DATA_WIDTH-1:0]  s_axis_tdata,
    output logic                   s_axis_tready,
    input  logic                   s_axis_tvalid,

    output logic [7:0] m_axis_tdata,
    input  logic       m_axis_tready,
    output logic       m_axis_tvalid
);
    logic [7:0] mem [BYTES_COUNT];
    logic [ADDRSIZE-1:0] r_ptr;

    always_ff @(posedge clk or negedge arst_n) begin
        if (!arst_n) begin
            r_ptr <= BYTES_COUNT;
        end
        else begin
            if (s_axis_tready && s_axis_tvalid) begin
                r_ptr <= 'b0;
            end

            if (m_axis_tready && m_axis_tvalid) begin
                r_ptr <= r_ptr + 1'b1;
            end
        end
    end

    assign s_axis_tready = (r_ptr == BYTES_COUNT);
    assign m_axis_tvalid = !s_axis_tready;

    assign m_axis_tdata = mem[r_ptr];

    genvar i;
    generate
        for(i = 0; i < BYTES_COUNT; i = i + 1) begin
            always_ff @(posedge clk) begin
                if (s_axis_tready && s_axis_tvalid) begin
                    mem[i] <= s_axis_tdata[i*8 +: 8];
                end
            end
        end
    endgenerate
    
endmodule
