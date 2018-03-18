
$(document).ready(function(){

    var step_num = 0;


    console.log("TESTING TIME");
    var updateInterval = 1000; // In Milleseconds
    var updateRunning = true;
    var step_size = 1;

    var updateIntevalID = setInterval(function(){ if(updateRunning){updateStep();}}, updateInterval);


    var current_step = null;

    function updateStep(){
        var route = "/get_step";
        if(current_step !== null){
            current_step += step_size;
            route += '?step_num=' + current_step;
        }
        getFormatted(route, function (data,status) {

            if (status == 'success'){ 
                console.log("Step Updated");
                console.log(data);
                avg_oxygen = data.avg_oxygen_pressure;
                avg_carbonDioxide = data.avg_carbonDioxide;
                total_water= data.total_water;
                current_step = data.step_num;
                updateBarChart(avg_oxygen,avg_carbonDioxide,total_water);
                updateSolarDay(data.step_num)
                
            }
        });
     
    }

    $('#dashboard-pause').click(function(){

    })

    function updateSolarDay(date){
        document.getElementById('currentDate-id').innerHTML = "Mars: Sol " + Math.floor(1+(date / 24.65));
    }


});


