module top #(
    // ===== Clock / UART =====
    parameter int FCLK            = 50_000_000,  // system clock frequency [Hz]
    parameter int UART_BAUD       = 115_200,     // UART line baud rate
    parameter int UART_OVERSAMPLE = 8,           // UART RX/TX oversampling factor

    // ===== FFT =====
    parameter int N               = 512,  // FFT length (power of two)
    parameter int WIDTH           = 16,   // sample width, per I/Q component [bits]
    parameter int INTEGER_WIDTH   = 2,    // integer bits per component
    parameter int ROUND           = 1,    // round (1) instead of truncate (0) after scaling
    parameter int RADIX_OUT_SCALE = 1,    // 1/2 output scaling per radix-2 stage
    parameter int SATURATION      = 1,    // saturate (1) instead of wrap (0) on overflow

    // ===== Derived =====
    parameter int SAMPLE_WIDTH  = 2*WIDTH,                                  // I+Q combined width
    parameter int UART_PRESCALE = FCLK / (UART_BAUD * UART_OVERSAMPLE)      // UART clock divider
) (
    input  logic clk,
    input  logic arst_n,

    output logic txd,
    input  logic rxd
);
   
    logic rst;
    assign rst = !arst_n; 

    // UART IN
    logic uart_m_axis_tvalid;
    logic uart_m_axis_tready;
    logic [7:0] uart_m_axis_tdata;

    uart #(
        .DATA_WIDTH(8)
    ) uart_in (
        .clk              (clk                 ),
        .rst              (rst                 ),

        .s_axis_tdata     (),
        .s_axis_tvalid    (),
        .s_axis_tready    (),

        .m_axis_tdata     (uart_m_axis_tdata    ),
        .m_axis_tvalid    (uart_m_axis_tvalid   ),
        .m_axis_tready    (uart_m_axis_tready   ),

        .rxd              (rxd                  ),
        .txd              (),

        .tx_busy          (),
        .rx_busy          (),
        .rx_overrun_error (),
        .rx_frame_error   (),

        .prescale (UART_PRESCALE)
    );

    // BYTE PACKER
    logic [SAMPLE_WIDTH-1:0] byte_packer_m_axis_tdata;
    logic byte_packer_m_axis_tready;
    logic byte_packer_m_axis_tvalid;

    byte_packer #(
        .DATA_WIDTH(SAMPLE_WIDTH)
    ) byte_packer_inst (
        .clk           (clk          ),
        .arst_n        (arst_n       ),

        .s_axis_tdata  (uart_m_axis_tdata        ),
        .s_axis_tready (uart_m_axis_tready       ),
        .s_axis_tvalid (uart_m_axis_tvalid       ),

        .m_axis_tdata  (byte_packer_m_axis_tdata ),
        .m_axis_tready (byte_packer_m_axis_tready),
        .m_axis_tvalid (byte_packer_m_axis_tvalid)
    );

    // FFT TOP
    logic [SAMPLE_WIDTH-1:0] fft_m_tdata;
    logic fft_m_tvalid;
    logic fft_m_tready;

    fft #(
        .N(N),
        .WIDTH(WIDTH),
        .INTEGER_WIDTH(INTEGER_WIDTH),
        .ROUND(ROUND),
        .RADIX_OUT_SCALE(RADIX_OUT_SCALE),
        .SATURATION(SATURATION)
    ) fft_top (
        .clk      (clk     ),
        .arst_n   (arst_n  ),

        .s_tdata  (byte_packer_m_axis_tdata),
        .s_tvalid (byte_packer_m_axis_tvalid),
        .s_tready (byte_packer_m_axis_tready),

        .m_tdata  (fft_m_tdata ),
        .m_tvalid (fft_m_tvalid),
        .m_tready (fft_m_tready)
    );

    // BYTE UNPACKER
    logic [7:0] byte_unpacker_m_axis_tdata;
    logic byte_unpacker_m_axis_tready;
    logic byte_unpacker_m_axis_tvalid;

    byte_unpacker #(
        .DATA_WIDTH(SAMPLE_WIDTH)
    ) byte_unpacker_inst (
        .clk           (clk          ),
        .arst_n        (arst_n       ),

        .s_axis_tdata  (fft_m_tdata  ),
        .s_axis_tready (fft_m_tready ),
        .s_axis_tvalid (fft_m_tvalid),

        .m_axis_tdata  (byte_unpacker_m_axis_tdata),
        .m_axis_tready (byte_unpacker_m_axis_tready),
        .m_axis_tvalid (byte_unpacker_m_axis_tvalid)
    );

    // UART OUT
    uart #(
        .DATA_WIDTH(8)
    ) uart_out (
        .clk              (clk             ),
        .rst              (rst             ),

        .s_axis_tdata     (byte_unpacker_m_axis_tdata),
        .s_axis_tvalid    (byte_unpacker_m_axis_tvalid),
        .s_axis_tready    (byte_unpacker_m_axis_tready),

        .m_axis_tdata     (                ),
        .m_axis_tvalid    (                ),
        .m_axis_tready    (                ),

        .rxd              (                ),
        .txd              (txd             ),

        .tx_busy          (                ),
        .rx_busy          (                ),
        .rx_overrun_error (                ),
        .rx_frame_error   (                ),

        .prescale         (UART_PRESCALE   )
    );

endmodule
