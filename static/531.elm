import Graphics.Input as I
import Window

fromJust d m = case m of
   Just v  -> v
   Nothing -> d

heading = [markdown|
<h1>5/3/1 Calculator</h1>
Uses percentages from [How to build pure strength](http://www.t-nation.com/free_online_article/sports_body_training_performance/how_to_build_pure_strength)
<h2>Enter your 1RMs:</h2>
|]

type Week = { num : Int, multipliers : [Float], reps : [Int] }
type Weight = Float

week1 : Week
week1 = { num = 1, multipliers = [0.65, 0.75, 0.85], reps = [5,5,5] }

week2 : Week
week2 = { num = 2, multipliers = [0.70, 0.80, 0.90], reps = [3,3,3] }

week3 : Week
week3 = { num = 3, multipliers = [0.75, 0.85, 0.95], reps = [5,3,1] }

week4 : Week
week4 = { num = 4, multipliers = [0.40, 0.50, 0.60], reps = [5,5,5] }

lifts = ["Overhead Press", "Bench Press", "Squat", "Deadlift"]
liftHeadings = flow down . map plainText <| lifts

replicate n v = if n == 0 then [] else v :: replicate (n-1) v

(weightFlds, weightSigs) = unzip . map I.field <| (replicate 4 "0 lbs")
(howManyMonthsInp, howManyMonthsSig) = I.field "1"

table : Int -> Int -> [[Element]] -> Element
table columnWidth rowHeight cells =
   let row = flow right . map (container columnWidth rowHeight middle)
   in  flow down (map row cells)

display_set : Int -> Weight -> Element
display_set rs w = 
   let s = show . round <| w
       reps = show rs
   in  plainText <| s ++ " lbs x " ++ reps ++ " reps"

-- display_set_cell takes the 1RMs, the multiplier, and the number of reps
--              and returns an Element with each set for each weight flowing down
display_set_cell : [Weight] -> Float -> Int -> Element
display_set_cell ws mult reps =
   let weights = map (\w -> w * mult) ws
       elems = map (display_set reps) weights
   in  flow down elems

-- display_week should return a row containing the Week "header", then each set for each weight in the week
display_week : [Weight] -> Week -> [Element]
display_week weights week = 
   let sets = zipWith (display_set_cell weights) week.multipliers week.reps
       weekHead = plainText <| "Week " ++ show week.num
   in  [ weekHead, liftHeadings ] ++ sets

display_531_month : Int -> [Weight] -> Element
display_531_month width weights =
   let headings  = map plainText ["Week", "Lifts", "Set 1", "Set 2", "Set 3"] -- headings : [Element]
       ws        = map (display_week weights) [week1, week2, week3, week4] -- [[Element]]
       cells     = [ headings ] ++ ws -- [[Element]]
       colWidth  = width `div` 4
   in  table colWidth 100 cells

display_month : [Weight] -> Int -> Int -> Element
display_month ws wdth mnum = 
   let weights = map (\w -> w + (toFloat mnum - 1.0) * 5.0) ws
   in  flow down [ text . bold . toText <| ("Month " ++ show mnum), display_531_month wdth weights ]

display_months : Int -> [Weight] -> num -> [Element]
display_months wdth ws num = map (display_month ws wdth) [1..num]

display_fields : String -> Element -> Element
display_fields ex field = flow right [ plainText (ex ++ ":   "), field ]

display_inputs : [Element] -> Element -> Element
display_inputs flds howMany= 
   let fldElems = zipWith display_fields lifts flds
       fst2  = take 2 fldElems
       snd2  = drop 2 fldElems
       months = [ plainText "How many months to generate:  ", howMany ]
       elems = intersperse (spacer 0 10) . map (flow right) <| [months, fst2, snd2]
   in  flow down elems

inputs : Signal Element
inputs = display_inputs <~ (combine weightFlds) ~ howManyMonthsInp

display : Int -> Element -> [String] -> String -> Element
display w inps ws howMany = 
   let fields = flow down [ inps, spacer 0 20 ]
       weights = map (fromJust 0.0 . readFloat) ws
       howManyMonths = fromJust 1 . readInt <| howMany
       tableWidth = w `div` 2
       elems = heading :: fields :: display_months tableWidth weights howManyMonths
       disp = flow down elems
       elemHeight = heightOf disp
   in  container (w - 50) elemHeight midTop disp

main = display <~ Window.width ~ inputs ~ (combine weightSigs) ~ howManyMonthsSig
