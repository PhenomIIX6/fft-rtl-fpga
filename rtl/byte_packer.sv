module byte_packer #(
    parameter DATA_WIDTH = 32,
    
    parameter BYTES_COUNT = int'($ceil(real'(DATA_WIDTH) / 8)),
    parameter ADDRSIZE = $clog2(BYTES_COUNT+1)
) (
    input  logic clk,
    input  logic arst_n,

    input  logic [7:0]  s_axis_tdata,
    output logic        s_axis_tready,
    input  logic        s_axis_tvalid,

    output logic [DATA_WIDTH-1:0] m_axis_tdata,
    input  logic                  m_axis_tready,
    output logic                  m_axis_tvalid
);
    logic [7:0] mem [BYTES_COUNT]; 
    logic [ADDRSIZE-1:0] w_ptr;

    always_ff @(posedge clk or negedge arst_n) begin
        if (!arst_n) begin
            w_ptr <= 'b0; 
        end
        else begin
            if (m_axis_tvalid && m_axis_tready) begin
                w_ptr <= 'b0;
            end

            if (s_axis_tready && s_axis_tvalid) begin
                mem[w_ptr] <= s_axis_tdata;
                w_ptr <= w_ptr + 1'b1;
            end
        end
    end

    assign m_axis_tvalid = (w_ptr == BYTES_COUNT);
    assign s_axis_tready = !m_axis_tvalid;

    genvar i;
    generate
        for(i = 0; i < BYTES_COUNT-1; i = i + 1) begin
            assign m_axis_tdata[i*8 +: 8] = mem[i];
        end
    endgenerate
    
    localparam LAST_WIDTH = ((DATA_WIDTH % 8) == 0) ? 8 : (DATA_WIDTH % 8);
    assign m_axis_tdata[DATA_WIDTH-1 -: LAST_WIDTH] = mem[BYTES_COUNT-1][0 +: LAST_WIDTH];

endmodule
