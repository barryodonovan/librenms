<?php
require 'includes/graphs/common.inc.php';
$rrdfilename = rrd_name($device['hostname'], 'ceragon-radio');
if (rrdtool_check_rrd_exists($rrdfilename)) {
    $rrd_options .= ' COMMENT:"  Now        Min         Max\r" ';
    $num_radios = explode(' ', $device[features])[0];
    $color = array("CC0000", "00CC00", "0000CC", "CCCCCC");    
    for ($i=1; $i <= $num_radios; $i++) {
        $rrd_options .= ' DEF:radio'.$i.'TxPower='.$rrdfilename.':radio'.$i.'TxPower:AVERAGE ';
        $rrd_options .= ' LINE1:radio'.$i.'TxPower#'.$color[$i-1].':"Radio '.$i.' RX Level\l" ';
        $rrd_options .= ' COMMENT:\u ';
        $rrd_options .= ' GPRINT:radio'.$i.'TxPower:LAST:"%3.2lf dBm" ';
        $rrd_options .= ' GPRINT:radio'.$i.'TxPower:MIN:"%3.2lf dBm" ';
        $rrd_options .= ' GPRINT:radio'.$i.'TxPower:MAX:"%3.2lf dBm\r" ';
    }
/*    $rrd_options .= ' DEF:radio1TxPower='.$rrdfilename.':radio1TxPower:AVERAGE ';
    $rrd_options .= ' DEF:radio2TxPower='.$rrdfilename.':radio2TxPower:AVERAGE ';
    $rrd_options .= ' LINE1:radio1TxPower#CC0000:"Radio 1 TX Power\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:radio1TxPower:LAST:"%3.2lf dBm" ';
    $rrd_options .= ' GPRINT:radio1TxPower:MIN:"%3.2lf dBm" ';
    $rrd_options .= ' GPRINT:radio1TxPower:MAX:"%3.2lf dBm\r" ';
    $rrd_options .= ' LINE1:radio2TxPower#00CC00:"Radio 2 TX Power\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:radio2TxPower:LAST:"%3.2lf dBm" ';
    $rrd_options .= ' GPRINT:radio2TxPower:MIN:"%3.2lf dBm" ';
    $rrd_options .= ' GPRINT:radio2TxPower:MAX:"%3.2lf dBm\r" ';
*/}
?>
