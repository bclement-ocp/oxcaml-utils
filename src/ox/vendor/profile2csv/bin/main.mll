{
type row =
  { time : float option
  ; alloc : int option
  ; top_heap : int option
  ; absolute_top_heap : int option
  ; counters : (string * int) list
  }

let csv_string s =
  match ignore (String.index s ',') with
  | exception Not_found -> s
  | () -> "\"" ^ s ^ "\""

let csv_counters counters =
  List.map
    (fun (name, value) -> name ^ "=" ^ string_of_int value)
    counters
  |> String.concat ","
  |> csv_string

let csv_float_option = function
  | None -> "0."
  | Some f -> string_of_float f

let csv_int_option = function
  | None -> "0"
  | Some i -> string_of_int i

let csv_row ppf { time; alloc; top_heap; absolute_top_heap; counters } =
  Format.fprintf ppf "%s,%s,%s,%s,%s"
    (csv_float_option time)
    (csv_int_option alloc)
    (csv_int_option top_heap)
    (csv_int_option absolute_top_heap)
    (csv_counters counters)

let empty_row =
  { time = None
  ; alloc = None
  ; top_heap = None
  ; absolute_top_heap = None
  ; counters = []
  }

type level =
  { name : string
  ; row  : row
  ; children : level list }

let rec csv_level prefix ppf { name; row; children } =
  let name = prefix ^ name in
  Format.fprintf ppf "@ %s,%a%a" (csv_string name) csv_row row
    (Format.pp_print_list ~pp_sep:Format.pp_print_nothing
        (csv_level (name ^ "/")))
    children

let csv_levels ppf levels =
  Format.fprintf ppf "@[<v>pass name,time,alloc,top heap,absolute top heap,counters%a@]"
    (Format.pp_print_list ~pp_sep:Format.pp_print_nothing
      (csv_level "")) levels

type stack =
  { depth : int
  ; current : level
  ; parents : level list }

let empty =
  { depth = 0
  ; current = { name = "top"; row = empty_row; children = [] }
  ; parents = [] }

let pop_level { depth; current ; parents } =
  match parents with
  | [] ->
    { depth; current ; parents }
  | hd :: tl ->
    let current = { current with children = List.rev current.children } in
    { depth = depth - 1
    ; current = { hd with children = current :: hd.children }
    ; parents = tl }

let rec close stack =
  match stack.parents with
  | [] -> List.rev stack.current.children
  | _ :: _ -> close (pop_level stack)

let push_level current stack =
  { depth = stack.depth + 1
  ; current
  ; parents = stack.current :: stack.parents }

let add_row stack indent row name =
  let indent = ref indent in
  let stack = ref stack in
  while !indent < (!stack).depth do
    stack := pop_level !stack;
  done;
  push_level { name ; row ; children = [] } !stack

type error =
  | Illegal_character

exception Error of error

let illegal_character () =
  raise (Error Illegal_character)

let report_error lexbuf msg =
  let startp = Lexing.lexeme_start_p lexbuf in
  prerr_string "Lexical error on line ";
  prerr_int startp.Lexing.pos_lnum;
  prerr_string ", around character ";
  prerr_int (startp.Lexing.pos_cnum - startp.Lexing.pos_bol);
  begin match msg with
    Illegal_character ->
      prerr_string ": illegal character"
  end;
  prerr_newline ()

}

let nl = '\n' | '\r' | "\r\n"

rule line indent stack = parse
  | "  "
    { line (indent + 1) stack lexbuf }
  | ('-'+ | (['0'-'9']+ ('.' ['0'-'9']*)? as time) 's')
    { let time = Option.map float_of_string time in
      let alloc = memory lexbuf in
      let top_heap = memory lexbuf in
      let absolute_top_heap = memory lexbuf in
      let counters, pass_name = counters_and_pass_name lexbuf in
      let stack =
        add_row stack indent
          { time; alloc; top_heap; absolute_top_heap; counters }
          pass_name
      in
      line 0 stack lexbuf }
  | ' '+ nl
    { Lexing.new_line lexbuf; line 0 stack lexbuf }
  | ' '* eof
    { close stack }
  | ' ' ([^' ' '\r' '\n'] [^'\r' '\n']+ as pass_name) nl
    { Lexing.new_line lexbuf;
      let stack = add_row stack indent empty_row pass_name in
      line 0 stack lexbuf }
  | _ { illegal_character () }

and memory = parse
  | ' '+ '-'+
    { None }
  | ' '+ (['0'-'9']+ ('.' ['0'-'9']*)? as value)
    { let scale = unit lexbuf in
      Some (int_of_float (scale *. float_of_string value)) }
  | _ { illegal_character () }

and pass_name = parse
  | ([^'\r' '\n']+ as pass_name) nl
    { Lexing.new_line lexbuf;
      pass_name }
  | _ { illegal_character () }

and unit = parse
  | 'B' { 1. }
  | "kB" { 1024. }
  | "MB" { 1024. *. 1024. }
  | "GB" { 1024. *. 1024. *. 1024. }
  | _ { illegal_character () }

and counters_and_pass_name = shortest
  | ' '+ ([^'\r' '\n']+ as pass_name) nl
    { Lexing.new_line lexbuf;
      [], pass_name }
  | ' '+ '-'+ ' '+
    { [], pass_name lexbuf }
  | ' '+ '['
    { let counters = counters [] lexbuf in
      counters, pass_name lexbuf }
  | _+ nl { illegal_character () }

and counters pairs = parse
  | [';' ' ']+
    { counters pairs lexbuf }
  | ']' ' '+
    { List.rev pairs }
  | ((['a'-'z' 'A'-'Z' '_'] ['a'-'z' 'A'-'Z' '_' '-' '0'-'9']+) as name)
    ' '* '=' ' '*
    (('-'? ['0'-'9']+) as value)
    ';'?
    { counters ((name, int_of_string value) :: pairs) lexbuf }
  | _ { illegal_character () }

{
let file_prefix = "file="

let reorder_and_annotate levels =
  let other, levels =
    List.partition (fun { name; _ } -> name = "other") levels
  in
  let files, levels =
    List.partition_map
      (fun ({ name; _ } as level) ->
        if String.starts_with ~prefix:file_prefix name
        then
          let name =
            String.sub name
              (String.length file_prefix)
              (String.length name - String.length file_prefix)
          in
          Either.Left { level with name }
        else
          Either.Right level)
      levels
  in
  let files, other =
    match files with
    | _ :: _ -> files, levels @ other
    | [] -> levels, other
  in
  let files =
    List.map
      (fun ({ name; _ } as level) -> { level with name = file_prefix ^ name })
      files
  in
  files @ other

let () =
  let lexbuf = Lexing.from_channel stdin in
  match line 0 empty lexbuf with
  | levels ->
    let levels = reorder_and_annotate levels in
    Format.printf "@[<v>%a@]@." csv_levels levels;
  | exception Error msg ->
    report_error lexbuf msg
}
